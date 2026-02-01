"""
NOAA APT (Automatic Picture Transmission) Weather Satellite Decoder
Decodes NOAA 15, 18, 19 weather satellite images on 137 MHz
"""

import numpy as np
from typing import Dict, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# NOAA APT constants
NOAA_SAMPLE_RATE = 11025  # Hz
NOAA_CARRIER = 2400  # Hz subcarrier
NOAA_IMAGE_WIDTH = 2080  # pixels per line
NOAA_SYNC_A = [0, 0, 255, 255, 0, 0, 255, 255]  # Sync pattern for channel A
NOAA_SYNC_B = [255, 255, 0, 0, 255, 255, 0, 0]  # Sync pattern for channel B

# NOAA satellite frequencies
NOAA_FREQUENCIES = {
    'NOAA-15': 137.620e6,
    'NOAA-18': 137.9125e6,
    'NOAA-19': 137.100e6
}

@dataclass
class NOAAImage:
    """Decoded NOAA APT image"""
    satellite: str
    timestamp: datetime
    image_data: np.ndarray
    channel_a: np.ndarray  # Visible/IR
    channel_b: np.ndarray  # IR
    num_lines: int
    quality: float  # 0-1

class NOAAAPTDecoder:
    """NOAA APT weather satellite decoder"""

    def __init__(self):
        self.images: list = []
        self.current_image: Optional[NOAAImage] = None
        self.lines_decoded = 0

    def demodulate_am(self, iq_samples: np.ndarray) -> np.ndarray:
        """Demodulate AM signal to audio"""
        # Compute magnitude (AM demodulation)
        audio = np.abs(iq_samples)

        # Lowpass filter to remove high frequency noise
        from scipy import signal
        b, a = signal.butter(5, 4200 / (NOAA_SAMPLE_RATE / 2), 'low')
        audio = signal.filtfilt(b, a, audio)

        return audio

    def resample(self, audio: np.ndarray, from_rate: int, to_rate: int = NOAA_SAMPLE_RATE) -> np.ndarray:
        """Resample audio to target sample rate"""
        from scipy import signal
        num_samples = int(len(audio) * to_rate / from_rate)
        return signal.resample(audio, num_samples)

    def find_sync(self, line_data: np.ndarray) -> Optional[int]:
        """Find sync pattern in line data"""
        sync_pattern = np.array(NOAA_SYNC_A, dtype=float)

        # Normalize
        line_norm = (line_data - np.min(line_data)) / (np.max(line_data) - np.min(line_data)) * 255

        # Cross-correlate to find sync
        correlation = np.correlate(line_norm, sync_pattern, mode='valid')
        max_corr_idx = np.argmax(correlation)

        # Check if correlation is strong enough
        if correlation[max_corr_idx] > len(sync_pattern) * 200:
            return max_corr_idx
        return None

    def decode_line(self, audio_line: np.ndarray) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Decode one APT image line into channel A and B"""
        if len(audio_line) < NOAA_IMAGE_WIDTH:
            return None

        # Find sync position
        sync_pos = self.find_sync(audio_line)
        if sync_pos is None:
            return None

        # Extract image data after sync
        line_start = sync_pos + len(NOAA_SYNC_A)
        if line_start + 2080 > len(audio_line):
            return None

        line_data = audio_line[line_start:line_start + 2080]

        # Normalize to 0-255
        line_norm = (line_data - np.min(line_data)) / (np.max(line_data) - np.min(line_data)) * 255
        line_norm = line_norm.astype(np.uint8)

        # Split into channel A (first 1040 pixels) and channel B (last 1040 pixels)
        channel_a = line_norm[:1040]
        channel_b = line_norm[1040:]

        return channel_a, channel_b

    def decode_pass(self, iq_samples: np.ndarray, sample_rate: int, satellite: str = "NOAA") -> Optional[NOAAImage]:
        """Decode a full satellite pass"""
        logger.info(f"Decoding {satellite} APT transmission...")

        # Demodulate
        audio = self.demodulate_am(iq_samples)

        # Resample to APT rate if needed
        if sample_rate != NOAA_SAMPLE_RATE:
            audio = self.resample(audio, sample_rate, NOAA_SAMPLE_RATE)

        # Each line is 0.5 seconds at APT rate
        samples_per_line = int(NOAA_SAMPLE_RATE * 0.5)
        num_lines = len(audio) // samples_per_line

        logger.info(f"Processing {num_lines} scan lines...")

        channel_a_lines = []
        channel_b_lines = []
        decoded_lines = 0

        for i in range(num_lines):
            line_start = i * samples_per_line
            line_end = line_start + samples_per_line
            audio_line = audio[line_start:line_end]

            result = self.decode_line(audio_line)
            if result:
                ch_a, ch_b = result
                channel_a_lines.append(ch_a)
                channel_b_lines.append(ch_b)
                decoded_lines += 1

        if decoded_lines == 0:
            logger.warning("No valid lines decoded")
            return None

        # Stack lines into images
        channel_a_img = np.vstack(channel_a_lines)
        channel_b_img = np.vstack(channel_b_lines)

        # Create combined image
        full_image = np.hstack([channel_a_img, channel_b_img])

        # Calculate quality score
        quality = decoded_lines / num_lines

        image = NOAAImage(
            satellite=satellite,
            timestamp=datetime.now(),
            image_data=full_image,
            channel_a=channel_a_img,
            channel_b=channel_b_img,
            num_lines=decoded_lines,
            quality=quality
        )

        self.images.append(image)
        logger.info(f"Decoded {decoded_lines} lines (quality: {quality*100:.1f}%)")

        return image

    def save_image(self, image: NOAAImage, filename: str):
        """Save decoded image to file"""
        from PIL import Image
        img = Image.fromarray(image.image_data)
        img.save(filename)
        logger.info(f"Saved image to {filename}")

    def get_statistics(self) -> Dict[str, Any]:
        """Get decoder statistics"""
        return {
            'total_images': len(self.images),
            'satellites': list(set(img.satellite for img in self.images)),
            'avg_quality': np.mean([img.quality for img in self.images]) if self.images else 0
        }
