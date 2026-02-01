"""
Advanced spectrum analysis module for SDR-MCP
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from scipy import signal
from scipy.fftpack import fft, fftshift, fftfreq
import asyncio
import json
import os
import wave
from datetime import datetime
from collections import deque
from pathlib import Path

@dataclass
class Signal:
    """Detected signal information"""
    frequency: float
    power: float
    bandwidth: float
    snr: float
    modulation_hint: Optional[str] = None
    confidence: float = 0.0

@dataclass
class SpectrumFrame:
    """Single spectrum analysis frame"""
    timestamp: datetime
    center_freq: float
    sample_rate: float
    frequencies: np.ndarray
    power_db: np.ndarray
    peak_power: float
    noise_floor: float
    detected_signals: List[Signal]

class SpectrumAnalyzer:
    """Advanced spectrum analysis with signal detection and classification"""
    
    def __init__(self, 
                 fft_size: int = 2048,
                 window_type: str = 'blackman-harris',
                 overlap: float = 0.5,
                 averaging_alpha: float = 0.1):
        self.fft_size = fft_size
        self.window_type = window_type
        self.overlap = overlap
        self.averaging_alpha = averaging_alpha
        
        # Window function
        self.window = self._get_window(window_type, fft_size)
        
        # Averaging buffers
        self.averaged_spectrum = None
        self.peak_hold = None
        
        # Waterfall data
        self.waterfall_history = deque(maxlen=100)
        
        # Signal detection parameters
        self.noise_floor_db = -100
        self.signal_threshold_db = 10  # dB above noise floor
        
    def _get_window(self, window_type: str, size: int) -> np.ndarray:
        """Get window function"""
        windows = {
            'hamming': signal.windows.hamming,
            'hann': signal.windows.hann,
            'blackman': signal.windows.blackman,
            'blackman-harris': signal.windows.blackmanharris,
            'flattop': signal.windows.flattop,
            'kaiser': lambda N: signal.windows.kaiser(N, beta=8.6),
        }
        
        if window_type in windows:
            return windows[window_type](size)
        else:
            return np.ones(size)  # Rectangular window
            
    def compute_psd(self, samples: np.ndarray,
                    sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
        """Compute Power Spectral Density"""
        # Ensure we have the right number of samples
        num_samples = len(samples)

        # Regenerate window if sample size changed
        if num_samples != len(self.window):
            self.window = self._get_window(self.window_type, num_samples)
            self.fft_size = num_samples

        # Apply window
        windowed = samples * self.window

        # Compute FFT
        spectrum = fftshift(fft(windowed))

        # Compute power in dB
        power = np.abs(spectrum) ** 2
        power_db = 10 * np.log10(power + 1e-10)

        # Normalize for window power
        window_power = np.sum(self.window ** 2)
        power_db -= 10 * np.log10(window_power)

        # Generate frequency array
        freqs = fftshift(fftfreq(self.fft_size, 1/sample_rate))

        return freqs, power_db
        
    def update_averaging(self, power_db: np.ndarray):
        """Update averaged spectrum and peak hold"""
        # Reset averaging buffers if size changed
        if self.averaged_spectrum is None or len(self.averaged_spectrum) != len(power_db):
            self.averaged_spectrum = power_db.copy()
            self.peak_hold = power_db.copy()
        else:
            # Exponential averaging
            self.averaged_spectrum = (self.averaging_alpha * power_db + 
                                     (1 - self.averaging_alpha) * self.averaged_spectrum)
            # Peak hold
            self.peak_hold = np.maximum(self.peak_hold, power_db)
            
    def estimate_noise_floor(self, power_db: np.ndarray, 
                           percentile: float = 20) -> float:
        """Estimate noise floor using percentile method"""
        return np.percentile(power_db, percentile)
        
    def detect_signals(self, freqs: np.ndarray, 
                      power_db: np.ndarray,
                      center_freq: float) -> List[Signal]:
        """Detect signals in spectrum"""
        signals = []
        
        # Estimate noise floor
        noise_floor = self.estimate_noise_floor(power_db)
        threshold = noise_floor + self.signal_threshold_db
        
        # Find peaks
        peaks, properties = signal.find_peaks(
            power_db, 
            height=threshold,
            distance=10,  # Minimum distance between peaks
            prominence=6   # Minimum prominence
        )
        
        # Analyze each peak
        for idx in peaks:
            # Estimate bandwidth (3dB down points)
            peak_power = power_db[idx]
            freq = center_freq + freqs[idx]
            
            # Find 3dB bandwidth
            left_idx = idx
            right_idx = idx
            cutoff = peak_power - 3
            
            while left_idx > 0 and power_db[left_idx] > cutoff:
                left_idx -= 1
            while right_idx < len(power_db)-1 and power_db[right_idx] > cutoff:
                right_idx += 1
                
            bandwidth = freqs[right_idx] - freqs[left_idx]
            snr = peak_power - noise_floor
            
            # Basic modulation hint based on bandwidth
            modulation_hint = self._guess_modulation(bandwidth, snr)
            
            signals.append(Signal(
                frequency=freq,
                power=peak_power,
                bandwidth=bandwidth,
                snr=snr,
                modulation_hint=modulation_hint,
                confidence=min(snr / 30, 1.0)  # Confidence based on SNR
            ))
            
        return signals
        
    def _guess_modulation(self, bandwidth: float, snr: float) -> str:
        """Guess modulation type based on bandwidth and SNR"""
        # This is a simplified heuristic
        if bandwidth < 200:  # Very narrow
            return "CW"
        elif bandwidth < 3000:  # Narrow
            return "NFM"
        elif bandwidth < 10000:  # Medium
            return "AM/NFM"
        elif bandwidth < 200000:  # Wide
            return "WFM"
        else:  # Very wide
            return "Digital/TV"
            
    def identify_known_signals(self, signals: List[Signal], 
                             center_freq: float) -> List[Signal]:
        """Identify known signal types based on frequency"""
        # Common frequencies and their uses
        known_signals = {
            # Aviation
            (108e6, 118e6): "Aviation AM",
            (118e6, 137e6): "Aviation AM",
            (1090e6, 1090e6): "ADS-B",
            (978e6, 978e6): "UAT",
            
            # Marine
            (156e6, 162e6): "Marine VHF",
            (161.975e6, 162.025e6): "AIS",
            
            # Amateur Radio
            (144e6, 148e6): "2m Amateur",
            (430e6, 440e6): "70cm Amateur",
            (14e6, 14.35e6): "20m Amateur",
            
            # Broadcast
            (88e6, 108e6): "FM Broadcast",
            (535e3, 1705e3): "AM Broadcast",
            
            # Emergency
            (150.8e6, 162.5e6): "Public Safety",
            
            # ISM
            (433.05e6, 434.79e6): "ISM 433",
            (902e6, 928e6): "ISM 900",
            (2.4e9, 2.5e9): "ISM 2.4G",
        }
        
        for sig in signals:
            for (low, high), description in known_signals.items():
                if low <= sig.frequency <= high:
                    sig.modulation_hint = f"{sig.modulation_hint or ''} ({description})"
                    sig.confidence = min(sig.confidence + 0.2, 1.0)
                    break
                    
        return signals
        
    async def analyze_spectrum(self, samples: np.ndarray,
                             sample_rate: float,
                             center_freq: float) -> SpectrumFrame:
        """Perform complete spectrum analysis"""
        # Compute PSD
        freqs, power_db = self.compute_psd(samples, sample_rate)
        
        # Update averaging
        self.update_averaging(power_db)
        
        # Detect signals
        signals = self.detect_signals(freqs, power_db, center_freq)
        
        # Identify known signals
        signals = self.identify_known_signals(signals, center_freq)
        
        # Update waterfall
        self.waterfall_history.append(power_db)
        
        # Create frame
        frame = SpectrumFrame(
            timestamp=datetime.now(),
            center_freq=center_freq,
            sample_rate=sample_rate,
            frequencies=center_freq + freqs,
            power_db=power_db,
            peak_power=np.max(power_db),
            noise_floor=self.estimate_noise_floor(power_db),
            detected_signals=signals
        )
        
        return frame
        
    def get_waterfall_data(self, num_lines: Optional[int] = None) -> np.ndarray:
        """Get waterfall display data"""
        if not self.waterfall_history:
            return np.array([])
            
        data = np.array(list(self.waterfall_history))
        if num_lines and len(data) > num_lines:
            return data[-num_lines:]
        return data
        
    def reset_averaging(self):
        """Reset averaging buffers"""
        self.averaged_spectrum = None
        self.peak_hold = None
        
    def clear_peak_hold(self):
        """Clear peak hold buffer"""
        if self.averaged_spectrum is not None:
            self.peak_hold = self.averaged_spectrum.copy()

class SignalRecorder:
    """Record IQ samples and spectrum data"""

    def __init__(self, base_path: str = None):
        # Use /tmp/sdr_recordings as default - always writable
        if base_path is None:
            base_path = "/tmp/sdr_recordings"
        self.base_path = base_path
        self.current_recording = None
        self.recording_metadata = {}
        
    async def start_recording(self, 
                            center_freq: float,
                            sample_rate: float,
                            gain: float,
                            description: str = "") -> str:
        """Start a new recording"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        recording_id = f"recording_{timestamp}_{int(center_freq/1e6)}MHz"
        
        self.recording_metadata = {
            "id": recording_id,
            "start_time": datetime.now().isoformat(),
            "center_freq": center_freq,
            "sample_rate": sample_rate,
            "gain": gain,
            "description": description,
            "samples_recorded": 0
        }

        # Create recording file
        os.makedirs(self.base_path, exist_ok=True)
        self.current_recording = open(
            f"{self.base_path}/{recording_id}.iq", 
            "wb"
        )
        
        return recording_id
        
    async def add_samples(self, samples: np.ndarray):
        """Add samples to current recording"""
        if self.current_recording:
            # Convert to interleaved I/Q format
            iq_data = np.empty(len(samples) * 2, dtype=np.float32)
            iq_data[0::2] = samples.real
            iq_data[1::2] = samples.imag
            
            # Write to file
            iq_data.tofile(self.current_recording)
            self.recording_metadata["samples_recorded"] += len(samples)
            
    async def stop_recording(self) -> Dict[str, Any]:
        """Stop current recording"""
        if self.current_recording:
            self.current_recording.close()
            self.current_recording = None
            
            # Save metadata
            self.recording_metadata["end_time"] = datetime.now().isoformat()
            self.recording_metadata["duration"] = (
                datetime.fromisoformat(self.recording_metadata["end_time"]) -
                datetime.fromisoformat(self.recording_metadata["start_time"])
            ).total_seconds()
            
            # Write metadata file
            metadata_file = f"{self.base_path}/{self.recording_metadata['id']}.json"
            with open(metadata_file, "w") as f:
                json.dump(self.recording_metadata, f, indent=2)
                
            return self.recording_metadata
            
        return {}

class FrequencyScanner:
    """Scan frequency ranges for signals"""
    
    def __init__(self, analyzer: SpectrumAnalyzer):
        self.analyzer = analyzer
        self.scan_results = []
        
    async def scan_range(self, 
                        sdr_device,
                        start_freq: float,
                        stop_freq: float,
                        step: float,
                        dwell_time: float = 0.1) -> List[Dict[str, Any]]:
        """Scan a frequency range"""
        self.scan_results = []
        
        current_freq = start_freq
        while current_freq <= stop_freq:
            # Tune to frequency
            await sdr_device.set_frequency(current_freq)
            await asyncio.sleep(0.05)  # Settling time
            
            # Capture samples
            num_samples = int(sdr_device.sample_rate * dwell_time)
            samples = await sdr_device.read_samples(num_samples)
            
            # Analyze
            frame = await self.analyzer.analyze_spectrum(
                samples, 
                sdr_device.sample_rate,
                current_freq
            )
            
            # Store results
            if frame.detected_signals:
                self.scan_results.append({
                    "frequency": current_freq,
                    "timestamp": frame.timestamp.isoformat(),
                    "signals": [
                        {
                            "frequency": sig.frequency,
                            "power": sig.power,
                            "bandwidth": sig.bandwidth,
                            "snr": sig.snr,
                            "type": sig.modulation_hint
                        }
                        for sig in frame.detected_signals
                    ]
                })
                
            current_freq += step
            
        return self.scan_results
        
    def get_activity_summary(self) -> Dict[str, Any]:
        """Get summary of scan results"""
        if not self.scan_results:
            return {"message": "No scan data available"}
            
        total_signals = sum(len(r["signals"]) for r in self.scan_results)
        
        # Group by signal type
        signal_types = {}
        for result in self.scan_results:
            for sig in result["signals"]:
                sig_type = sig.get("type", "Unknown")
                if sig_type not in signal_types:
                    signal_types[sig_type] = 0
                signal_types[sig_type] += 1
                
        return {
            "scan_points": len(self.scan_results),
            "total_signals": total_signals,
            "signal_types": signal_types,
            "strongest_signal": self._find_strongest_signal()
        }
        
    def _find_strongest_signal(self) -> Optional[Dict[str, Any]]:
        """Find the strongest signal from scan"""
        strongest = None
        max_power = -200

        for result in self.scan_results:
            for sig in result["signals"]:
                if sig["power"] > max_power:
                    max_power = sig["power"]
                    strongest = sig

        return strongest

class AudioRecorder:
    """Record demodulated audio from FM/AM signals"""

    def __init__(self, base_path: str = None):
        # Use /tmp/sdr_recordings as default - always writable
        if base_path is None:
            base_path = "/tmp/sdr_recordings"
        self.base_path = base_path
        self.current_recording = None
        self.recording_metadata = {}
        self.audio_rate = 48000  # Standard audio sample rate

        # For smooth audio - track previous samples for continuity
        self.last_phase = 0.0
        self.dc_filter_state = 0.0

        # AGC (Automatic Gain Control) instead of per-chunk normalization
        self.agc_gain = 0.1
        self.agc_target = 0.3  # Target RMS level (leave headroom)

    def _fm_demodulate(self, samples: np.ndarray) -> np.ndarray:
        """FM demodulation using phase difference with continuity"""
        # Calculate instantaneous phase
        phase = np.unwrap(np.angle(samples))

        # Use last phase from previous chunk for continuity
        if self.last_phase != 0.0:
            # Adjust phase to be continuous with previous chunk
            phase_offset = self.last_phase - phase[0]
            phase = phase + phase_offset

        # Store last phase for next chunk
        self.last_phase = phase[-1]

        # Phase difference (derivative) - this is the demodulated signal
        phase_diff = np.diff(phase)

        # Pad to maintain same length
        phase_diff = np.append(phase_diff, phase_diff[-1])

        return phase_diff

    def _am_demodulate(self, samples: np.ndarray) -> np.ndarray:
        """AM demodulation using envelope detection with DC filter"""
        # Calculate envelope (magnitude)
        envelope = np.abs(samples)

        # High-pass filter to remove DC (continuous across chunks)
        alpha = 0.95  # Filter coefficient
        filtered = np.zeros_like(envelope)
        filtered[0] = envelope[0] - self.dc_filter_state

        for i in range(1, len(envelope)):
            filtered[i] = alpha * (filtered[i-1] + envelope[i] - envelope[i-1])

        self.dc_filter_state = envelope[-1]

        return filtered

    def _resample(self, audio: np.ndarray,
                  original_rate: float,
                  target_rate: float) -> np.ndarray:
        """Resample audio to target rate using polyphase filtering"""
        if original_rate == target_rate:
            return audio

        # Use scipy's resample_poly for better quality (less artifacts)
        from scipy.signal import resample_poly

        # Find greatest common divisor for efficient resampling
        from math import gcd
        ratio_num = int(target_rate)
        ratio_den = int(original_rate)
        common = gcd(ratio_num, ratio_den)
        up = ratio_num // common
        down = ratio_den // common

        # Resample using polyphase filtering
        return resample_poly(audio, up, down)

    def _apply_deemphasis(self, audio: np.ndarray,
                          sample_rate: float,
                          tau: float = 75e-6) -> np.ndarray:
        """Apply FM de-emphasis filter (75μs for US, 50μs for EU)"""
        # De-emphasis filter time constant
        # H(s) = 1 / (1 + s*tau)

        d = sample_rate * tau
        x = np.exp(-1.0 / d)

        # Simple IIR filter
        output = np.zeros_like(audio)
        output[0] = audio[0]

        for i in range(1, len(audio)):
            output[i] = audio[i] * (1 - x) + output[i-1] * x

        return output

    async def start_recording(self,
                              center_freq: float,
                              sample_rate: float,
                              modulation: str = "FM",
                              description: str = "") -> str:
        """Start a new audio recording"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        recording_id = f"audio_{timestamp}_{int(center_freq/1e6)}MHz_{modulation}"

        # Reset state variables for new recording
        self.last_phase = 0.0
        self.dc_filter_state = 0.0
        self.agc_gain = 0.1

        self.recording_metadata = {
            "id": recording_id,
            "start_time": datetime.now().isoformat(),
            "center_freq": center_freq,
            "sample_rate": sample_rate,
            "modulation": modulation.upper(),
            "description": description,
            "audio_rate": self.audio_rate,
            "samples_recorded": 0
        }

        # Create recording directory
        os.makedirs(self.base_path, exist_ok=True)

        # Create WAV file
        wav_path = f"{self.base_path}/{recording_id}.wav"
        self.current_recording = wave.open(wav_path, 'wb')
        self.current_recording.setnchannels(1)  # Mono
        self.current_recording.setsampwidth(2)  # 16-bit
        self.current_recording.setframerate(self.audio_rate)

        return recording_id

    async def add_samples(self, samples: np.ndarray,
                         sample_rate: float,
                         modulation: str = "FM"):
        """Demodulate and add audio samples to recording"""
        if not self.current_recording:
            return

        # Demodulate based on type
        if modulation.upper() == "FM":
            audio = self._fm_demodulate(samples)
            # Apply de-emphasis for FM broadcast
            audio = self._apply_deemphasis(audio, sample_rate)
        elif modulation.upper() == "AM":
            audio = self._am_demodulate(samples)
        else:
            # Default to FM
            audio = self._fm_demodulate(samples)

        # Resample to audio rate
        audio = self._resample(audio, sample_rate, self.audio_rate)

        # Apply AGC (Automatic Gain Control) for smooth volume
        if len(audio) > 0:
            # Calculate RMS level
            rms = np.sqrt(np.mean(audio ** 2))

            # Update gain smoothly
            if rms > 0:
                target_gain = self.agc_target / rms
                # Smooth gain changes to avoid artifacts
                self.agc_gain = 0.9 * self.agc_gain + 0.1 * target_gain
                # Limit gain to prevent excessive amplification
                self.agc_gain = min(self.agc_gain, 10.0)

            # Apply gain
            audio = audio * self.agc_gain

            # Soft clipping to prevent hard clipping artifacts
            audio = np.tanh(audio)

        # Convert to 16-bit PCM
        audio_int16 = (audio * 32767 * 0.9).astype(np.int16)  # 0.9 for headroom

        # Write to WAV file
        self.current_recording.writeframes(audio_int16.tobytes())
        self.recording_metadata["samples_recorded"] += len(audio_int16)

    async def stop_recording(self) -> Dict[str, Any]:
        """Stop current audio recording"""
        if self.current_recording:
            self.current_recording.close()
            self.current_recording = None

            # Save metadata
            self.recording_metadata["end_time"] = datetime.now().isoformat()
            self.recording_metadata["duration"] = (
                datetime.fromisoformat(self.recording_metadata["end_time"]) -
                datetime.fromisoformat(self.recording_metadata["start_time"])
            ).total_seconds()

            # Write metadata file
            metadata_file = f"{self.base_path}/{self.recording_metadata['id']}.json"
            with open(metadata_file, "w") as f:
                json.dump(self.recording_metadata, f, indent=2)

            return self.recording_metadata

        return {}