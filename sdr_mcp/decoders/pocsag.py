"""
POCSAG Pager Decoder
Decodes POCSAG 512/1200/2400 baud pager messages
"""

import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# POCSAG constants
POCSAG_SYNC = 0x7CD215D8
POCSAG_IDLE = 0x7A89C197
POCSAG_BATCH_SIZE = 16  # 16 codewords per batch

@dataclass
class POCSAGMessage:
    """Decoded POCSAG pager message"""
    address: int
    function: int  # 0-3
    message: str
    timestamp: datetime
    bitrate: int
    numeric: bool

class POCSAGDecoder:
    """POCSAG pager protocol decoder"""

    def __init__(self):
        self.messages: List[POCSAGMessage] = []
        self.message_count = 0

    def decode_codeword(self, codeword: int) -> Optional[Dict[str, Any]]:
        """Decode a single POCSAG codeword"""
        # Check parity
        if not self._check_parity(codeword):
            return None

        # Bit 0 indicates address (0) or message (1)
        is_message = (codeword >> 31) & 1

        if is_message:
            # Message codeword - extract 20 bits of data
            data = (codeword >> 11) & 0xFFFFF
            return {'type': 'message', 'data': data}
        else:
            # Address codeword
            address = (codeword >> 13) & 0x3FFFF  # 18 bits
            function = (codeword >> 11) & 0x3      # 2 bits
            return {'type': 'address', 'address': address, 'function': function}

    def _check_parity(self, codeword: int) -> bool:
        """Check BCH parity of codeword"""
        # Simple parity check - count bits
        bits = bin(codeword).count('1')
        return (bits % 2) == 0

    def decode_message_data(self, data_words: List[int], numeric: bool = False) -> str:
        """Decode message data from multiple codewords"""
        if numeric:
            # Numeric pager message (BCD encoding)
            return self._decode_numeric(data_words)
        else:
            # Alphanumeric message (7-bit ASCII)
            return self._decode_alphanumeric(data_words)

    def _decode_numeric(self, data_words: List[int]) -> str:
        """Decode numeric pager message"""
        result = []
        for word in data_words:
            # Extract BCD digits (4 bits each)
            for i in range(5):  # 5 digits per 20-bit word
                digit = (word >> (i * 4)) & 0xF
                if digit < 10:
                    result.append(str(digit))
                elif digit == 0xA:
                    result.append('*')
                elif digit == 0xB:
                    result.append('U')  # Urgency
                elif digit == 0xC:
                    result.append(' ')
                elif digit == 0xD:
                    result.append('-')
                elif digit == 0xE:
                    result.append('(')
                elif digit == 0xF:
                    result.append(')')
        return ''.join(result).strip()

    def _decode_alphanumeric(self, data_words: List[int]) -> str:
        """Decode alphanumeric pager message (7-bit ASCII)"""
        bits = []
        for word in data_words:
            # Extract 20 bits
            for i in range(20):
                bits.append((word >> i) & 1)

        # Convert to 7-bit ASCII characters
        result = []
        for i in range(0, len(bits) - 6, 7):
            char_bits = bits[i:i+7]
            char_val = sum(bit << idx for idx, bit in enumerate(char_bits))
            if 32 <= char_val <= 126:  # Printable ASCII
                result.append(chr(char_val))

        return ''.join(result).strip()

    def process_batch(self, codewords: List[int]) -> List[POCSAGMessage]:
        """Process a batch of POCSAG codewords"""
        messages = []
        current_address = None
        current_function = None
        message_words = []

        for codeword in codewords:
            if codeword == POCSAG_IDLE:
                continue

            decoded = self.decode_codeword(codeword)
            if not decoded:
                continue

            if decoded['type'] == 'address':
                # New message starting
                if current_address is not None and message_words:
                    # Save previous message
                    msg_text = self.decode_message_data(message_words, numeric=False)
                    if msg_text:
                        message = POCSAGMessage(
                            address=current_address,
                            function=current_function,
                            message=msg_text,
                            timestamp=datetime.now(),
                            bitrate=1200,  # Default
                            numeric=False
                        )
                        messages.append(message)
                        self.messages.append(message)
                        self.message_count += 1

                current_address = decoded['address']
                current_function = decoded['function']
                message_words = []

            elif decoded['type'] == 'message':
                message_words.append(decoded['data'])

        # Handle last message
        if current_address is not None and message_words:
            msg_text = self.decode_message_data(message_words, numeric=False)
            if msg_text:
                message = POCSAGMessage(
                    address=current_address,
                    function=current_function,
                    message=msg_text,
                    timestamp=datetime.now(),
                    bitrate=1200,
                    numeric=False
                )
                messages.append(message)
                self.messages.append(message)
                self.message_count += 1

        return messages

    def get_recent_messages(self, max_age_seconds: int = 300) -> List[Dict[str, Any]]:
        """Get messages from the last N seconds"""
        now = datetime.now()
        recent = []

        for msg in self.messages:
            age = (now - msg.timestamp).total_seconds()
            if age < max_age_seconds:
                recent.append(asdict(msg))

        return recent

    def get_statistics(self) -> Dict[str, Any]:
        """Get decoder statistics"""
        return {
            'total_messages': self.message_count,
            'messages_stored': len(self.messages),
            'addresses_seen': len(set(msg.address for msg in self.messages))
        }
