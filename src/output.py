# src/output.py

from pylsl import StreamInfo, StreamOutlet
from config import Config

class UnityBridge:
    """
    Creates an LSL Outlet to broadcast the final therapeutic vector to the Unity VR engine.
    """
    def __init__(self):
        # 1. Define the outbound LSL Stream
        # We are sending 3 channels: [S_t, State_Integer, Dashboard_Score]
        self.info = StreamInfo(
            name=Config.OUT_STREAM_NAME,
            type=Config.OUT_STREAM_TYPE,
            channel_count=3,
            nominal_srate=Config.PIPELINE_RATE,
            channel_format='float32',
            source_id='python_fusion_engine'
        )
        
        self.outlet = StreamOutlet(self.info)
        print(f"[OUTPUT] Unity Bridge Initialized. Broadcasting '{Config.OUT_STREAM_NAME}'.")

    def broadcast_state(self, s_t: float, state_label: str, dashboard_score: float):
        """
        Encodes the string label into a float and pushes the vector to the network.
        """
        # Encode state for Unity C# to decode
        if state_label == "calm":
            state_enum = 0.0
        elif state_label == "stressed":
            state_enum = 1.0
        else: # ultra_stressed
            state_enum = 2.0
            
        vector = [s_t, state_enum, dashboard_score]
        self.outlet.push_sample(vector)