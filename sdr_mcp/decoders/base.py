"""
Base class for protocol decoders
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

class ProtocolDecoder(ABC):
    """Abstract base class for protocol decoders"""
    
    @abstractmethod
    def decode(self, data: Any) -> Optional[Dict[str, Any]]:
        """Decode protocol data"""
        pass
    
    @abstractmethod
    def get_statistics(self) -> Dict[str, Any]:
        """Get decoder statistics"""
        pass
