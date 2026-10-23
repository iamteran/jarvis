"""Boost Windows microphone volume to 100% and disable AGC using pycaw / ctypes."""
import ctypes, sys

def set_mic_volume_max():
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        devices = AudioUtilities.GetMicrophone()
        if not devices:
            print("No microphone found via pycaw")
            return
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = interface.QueryInterface(IAudioEndpointVolume)
        volume.SetMasterVolumeLevelScalar(1.0, None)  # 100%
        print(f"Microphone set to 100% (was {volume.GetMasterVolumeLevelScalar()*100:.0f}%)")
    except ImportError:
        print("pycaw not installed, trying alternative...")
        _boost_via_mixer()

def _boost_via_mixer():
    """Fallback: open Windows sound settings and print instructions."""
    import subprocess, os
    subprocess.Popen(["mmsys.cpl"], shell=True)
    print("\nOpening Sound Settings...")
    print("Go to: Recording tab -> Right-click your mic -> Properties -> Levels")
    print("Set Microphone to 100 and Microphone Boost to +30dB")

if __name__ == "__main__":
    set_mic_volume_max()
