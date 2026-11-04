"""
POCSAG Pager Decoder

Live decoding is performed by multimon-ng fed from rtl_fm (see
SDRMCPServer._pocsag_decoder_task). This module parses multimon-ng's text
output into messages and keeps the message history. The codeword-level
helpers at the bottom remain available for offline experiments.
"""

import logging
import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# POCSAG constants
POCSAG_SYNC = 0x7CD215D8
POCSAG_IDLE = 0x7A89C197
POCSAG_BATCH_SIZE = 16  # 16 codewords per batch
POCSAG_BAUD_RATES = (512, 1200, 2400)

# multimon-ng prints e.g.
#   POCSAG1200: Address: 1234567  Function: 0  Alpha:   HELLO WORLD
#   POCSAG512: Address:  123456  Function: 3  Numeric: 555-1234
#   POCSAG2400: Address: 1234567  Function: 2
MULTIMON_POCSAG_RE = re.compile(
    r"^POCSAG(?P<baud>512|1200|2400):\s+Address:\s*(?P<address>\d+)\s+Function:\s*(?P<function>\d)"
    r"(?:\s+(?P<kind>Alpha|Numeric|Skyper):\s*(?P<text>.*?))?\s*$"
)


@dataclass
class POCSAGMessage:
    """Decoded POCSAG pager message"""
    address: int
    function: int  # 0-3
    message: str
    timestamp: datetime
    bitrate: int
    numeric: bool
    message_type: str = "alpha"  # alpha | numeric | tone

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


class POCSAGDecoder:
    """POCSAG pager protocol decoder (multimon-ng output parser + message store)"""

    def __init__(self, max_messages: int = 1000):
        self.messages: Deque[POCSAGMessage] = deque(maxlen=max_messages)
        self.message_count = 0
        self.lines_seen = 0
        self.baud_rate: Optional[int] = None   # None = decode all rates
        self.frequency: Optional[float] = None

    # ------------------------------------------------------------ multimon-ng

    def parse_multimon_line(self, line: str) -> Optional[POCSAGMessage]:
        """Parse one line of multimon-ng output. Returns a message or None."""
        self.lines_seen += 1
        match = MULTIMON_POCSAG_RE.match(line.strip())
        if not match:
            return None

        kind = match.group("kind")
        text = (match.group("text") or "").strip()
        if kind == "Numeric":
            message_type = "numeric"
        elif kind in ("Alpha", "Skyper"):
            message_type = "alpha"
        else:
            message_type = "tone"

        message = POCSAGMessage(
            address=int(match.group("address")),
            function=int(match.group("function")),
            message=text,
            timestamp=datetime.now(),
            bitrate=int(match.group("baud")),
            numeric=(message_type == "numeric"),
            message_type=message_type,
        )
        self.messages.append(message)
        self.message_count += 1
        return message

    # ------------------------------------------------------------ queries

    def get_messages(self, max_age_seconds: Optional[int] = None,
                     limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return stored messages, newest last, optionally filtered by age and count."""
        now = datetime.now()
        items = list(self.messages)
        if max_age_seconds:
            items = [m for m in items if (now - m.timestamp).total_seconds() < max_age_seconds]
        if limit:
            items = items[-limit:]
        return [m.to_dict() for m in items]

    def get_recent_messages(self, max_age_seconds: int = 300) -> List[Dict[str, Any]]:
        """Get messages from the last N seconds"""
        return self.get_messages(max_age_seconds=max_age_seconds)

    def get_statistics(self) -> Dict[str, Any]:
        """Get decoder statistics"""
        messages = list(self.messages)
        return {
            'total_messages': self.message_count,
            'messages_stored': len(messages),
            'addresses_seen': len({m.address for m in messages}),
            'baud_rates_seen': sorted({m.bitrate for m in messages}),
            'alpha_messages': sum(1 for m in messages if m.message_type == "alpha"),
            'numeric_messages': sum(1 for m in messages if m.message_type == "numeric"),
            'tone_only_pages': sum(1 for m in messages if m.message_type == "tone"),
            'frequency_MHz': self.frequency / 1e6 if self.frequency else None,
        }

    def clear(self):
        self.messages.clear()

    # ------------------------------------------------------------ codeword helpers

    def decode_codeword(self, codeword: int) -> Optional[Dict[str, Any]]:
        """Decode a single POCSAG codeword"""
        if not self._check_parity(codeword):
            return None

        # Bit 31 indicates address (0) or message (1)
        is_message = (codeword >> 31) & 1

        if is_message:
            data = (codeword >> 11) & 0xFFFFF
            return {'type': 'message', 'data': data}
        address = (codeword >> 13) & 0x3FFFF  # 18 bits
        function = (codeword >> 11) & 0x3      # 2 bits
        return {'type': 'address', 'address': address, 'function': function}

    def _check_parity(self, codeword: int) -> bool:
        """Even-parity check of a codeword (the final BCH parity bit)"""
        bits = bin(codeword).count('1')
        return (bits % 2) == 0

    def decode_message_data(self, data_words: List[int], numeric: bool = False) -> str:
        """Decode message data from multiple codewords"""
        if numeric:
            return self._decode_numeric(data_words)
        return self._decode_alphanumeric(data_words)

    def _decode_numeric(self, data_words: List[int]) -> str:
        """Decode numeric pager message (BCD)"""
        table = {0xA: '*', 0xB: 'U', 0xC: ' ', 0xD: '-', 0xE: '(', 0xF: ')'}
        result = []
        for word in data_words:
            for i in range(5):  # 5 digits per 20-bit word
                digit = (word >> (i * 4)) & 0xF
                result.append(str(digit) if digit < 10 else table[digit])
        return ''.join(result).strip()

    def _decode_alphanumeric(self, data_words: List[int]) -> str:
        """Decode alphanumeric pager message (7-bit ASCII)"""
        bits = []
        for word in data_words:
            for i in range(20):
                bits.append((word >> i) & 1)

        result = []
        for i in range(0, len(bits) - 6, 7):
            char_bits = bits[i:i + 7]
            char_val = sum(bit << idx for idx, bit in enumerate(char_bits))
            if 32 <= char_val <= 126:
                result.append(chr(char_val))
        return ''.join(result).strip()

    def process_batch(self, codewords: List[int]) -> List[POCSAGMessage]:
        """Process a batch of POCSAG codewords (offline helper)"""
        messages = []
        current_address = None
        current_function = None
        message_words: List[int] = []

        def flush():
            if current_address is not None and message_words:
                msg_text = self.decode_message_data(message_words, numeric=False)
                if msg_text:
                    message = POCSAGMessage(
                        address=current_address,
                        function=current_function,
                        message=msg_text,
                        timestamp=datetime.now(),
                        bitrate=self.baud_rate or 1200,
                        numeric=False,
                    )
                    messages.append(message)
                    self.messages.append(message)
                    self.message_count += 1

        for codeword in codewords:
            if codeword == POCSAG_IDLE:
                continue
            decoded = self.decode_codeword(codeword)
            if not decoded:
                continue
            if decoded['type'] == 'address':
                flush()
                current_address = decoded['address']
                current_function = decoded['function']
                message_words = []
            else:
                message_words.append(decoded['data'])

        flush()
        return messages
