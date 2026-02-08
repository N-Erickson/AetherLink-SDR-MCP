"""
RTL_433 Decoder
Decodes ISM band devices (433MHz, 315MHz, 868MHz, 915MHz)
Common protocols: Weather stations, tire pressure monitors, doorbells, sensors
"""

import json
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)

# Common ISM band frequencies
ISM_FREQUENCIES = {
    "315MHz": 315e6,    # North America - car keys, garage doors
    "433MHz": 433.92e6,  # Europe/Asia - weather stations, sensors, doorbells
    "868MHz": 868e6,     # Europe - LoRa, wireless sensors
    "915MHz": 915e6,     # North America - LoRa, RFID, sensors
}

@dataclass
class RTL433Device:
    """Decoded device from rtl_433"""
    model: str
    id: Optional[int] = None
    channel: Optional[int] = None
    battery_ok: Optional[bool] = None
    temperature_C: Optional[float] = None
    temperature_F: Optional[float] = None
    humidity: Optional[int] = None
    wind_speed_kph: Optional[float] = None
    wind_dir_deg: Optional[float] = None
    rain_mm: Optional[float] = None
    pressure_hPa: Optional[float] = None
    rssi: Optional[float] = None
    snr: Optional[float] = None
    noise: Optional[float] = None
    frequency_MHz: Optional[float] = None
    timestamp: datetime = None
    raw_data: Dict[str, Any] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

class RTL433Decoder:
    """RTL_433 ISM band decoder using rtl_433 subprocess"""

    def __init__(self):
        self.devices: Dict[str, RTL433Device] = {}  # Key: "{model}_{id}"
        self.message_count = 0
        self.raw_messages: List[Dict[str, Any]] = []
        self.frequencies: List[float] = [433.92e6, 315e6]  # Default frequencies
        self.hop_interval: int = 30  # Default hop interval in seconds

    def set_frequencies(self, frequencies: List[float]):
        """Set frequencies to scan"""
        self.frequencies = frequencies
        logger.info(f"RTL_433 frequencies set to: {[f/1e6 for f in frequencies]} MHz")

    def set_hop_interval(self, interval: int):
        """Set frequency hop interval in seconds"""
        self.hop_interval = interval
        logger.info(f"RTL_433 hop interval set to: {interval} seconds")

    def parse_message(self, json_line: str) -> Optional[RTL433Device]:
        """Parse a JSON message from rtl_433"""
        try:
            data = json.loads(json_line)

            # Store raw message
            self.raw_messages.append(data)
            if len(self.raw_messages) > 1000:  # Keep last 1000
                self.raw_messages = self.raw_messages[-1000:]

            self.message_count += 1

            # Extract common fields
            model = data.get('model', 'Unknown')
            device_id = data.get('id')

            # Create device key
            device_key = f"{model}_{device_id}" if device_id else f"{model}_{self.message_count}"

            # Parse device data
            device = RTL433Device(
                model=model,
                id=device_id,
                channel=data.get('channel'),
                battery_ok=data.get('battery_ok') == 1 if 'battery_ok' in data else None,
                temperature_C=data.get('temperature_C'),
                temperature_F=data.get('temperature_F'),
                humidity=data.get('humidity'),
                wind_speed_kph=data.get('wind_avg_km_h') or data.get('wind_speed_kph'),
                wind_dir_deg=data.get('wind_dir_deg'),
                rain_mm=data.get('rain_mm'),
                pressure_hPa=data.get('pressure_hPa'),
                rssi=data.get('rssi'),
                snr=data.get('snr'),
                noise=data.get('noise'),
                frequency_MHz=data.get('freq'),
                timestamp=datetime.now(),
                raw_data=data
            )

            # Update device database
            self.devices[device_key] = device

            logger.debug(f"RTL_433: Decoded {model} (ID: {device_id})")
            return device

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse RTL_433 JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"Error parsing RTL_433 message: {e}")
            return None

    def get_device_list(self, max_age_seconds: int = 300) -> List[Dict[str, Any]]:
        """Get list of recently seen devices (last 5 minutes by default)"""
        now = datetime.now()
        recent_devices = []

        for device_key, device in self.devices.items():
            age = (now - device.timestamp).total_seconds()
            if age < max_age_seconds:
                device_dict = asdict(device)
                device_dict['age_seconds'] = int(age)
                recent_devices.append(device_dict)

        # Sort by most recent
        recent_devices.sort(key=lambda d: d['timestamp'], reverse=True)
        return recent_devices

    def get_statistics(self) -> Dict[str, Any]:
        """Get decoder statistics"""
        now = datetime.now()
        active_devices = sum(
            1 for device in self.devices.values()
            if (now - device.timestamp).total_seconds() < 300
        )

        # Count device types
        device_types = {}
        for device in self.devices.values():
            model = device.model
            device_types[model] = device_types.get(model, 0) + 1

        return {
            'total_messages': self.message_count,
            'total_devices_seen': len(self.devices),
            'active_devices': active_devices,
            'device_types': device_types,
            'frequencies_MHz': [f/1e6 for f in self.frequencies],
            'hop_interval_seconds': self.hop_interval
        }

    def get_device_summary(self, device) -> str:
        """Generate human-readable summary of a device

        Args:
            device: Either RTL433Device object or dict from asdict(RTL433Device)
        """
        # Handle both object and dict formats
        if isinstance(device, dict):
            get_attr = lambda key, default=None: device.get(key, default)
        else:
            get_attr = lambda key, default=None: getattr(device, key, default)

        model = get_attr('model', 'Unknown')
        summary = f"[{model}]"

        device_id = get_attr('id')
        if device_id:
            summary += f" ID:{device_id}"

        channel = get_attr('channel')
        if channel:
            summary += f" Ch:{channel}"

        # Add sensor readings
        readings = []
        temp_c = get_attr('temperature_C')
        if temp_c is not None:
            readings.append(f"{temp_c:.1f}°C")

        humidity = get_attr('humidity')
        if humidity is not None:
            readings.append(f"{humidity}% RH")

        wind_speed = get_attr('wind_speed_kph')
        if wind_speed is not None:
            readings.append(f"Wind:{wind_speed:.1f}kph")

        rain = get_attr('rain_mm')
        if rain is not None:
            readings.append(f"Rain:{rain}mm")

        pressure = get_attr('pressure_hPa')
        if pressure is not None:
            readings.append(f"Pressure:{pressure:.1f}hPa")

        if readings:
            summary += " | " + " ".join(readings)

        # Add signal info
        signal_info = []
        rssi = get_attr('rssi')
        if rssi is not None:
            signal_info.append(f"RSSI:{rssi:.1f}dB")

        snr = get_attr('snr')
        if snr is not None:
            signal_info.append(f"SNR:{snr:.1f}dB")

        freq_mhz = get_attr('frequency_MHz')
        if freq_mhz is not None:
            signal_info.append(f"{freq_mhz:.3f}MHz")

        if signal_info:
            summary += " | " + " ".join(signal_info)

        # Battery status
        battery_ok = get_attr('battery_ok')
        if battery_ok is not None:
            battery_status = "🔋" if battery_ok else "🪫"
            summary += f" {battery_status}"

        return summary

    def clear_devices(self):
        """Clear all stored devices"""
        self.devices.clear()
        logger.info("RTL_433: Cleared all devices")
