"""AEC verification: synthetic room by default; --acoustic uses local devices.

Only numeric summaries and reference-file transcripts are saved. Microphone
audio stays in RAM. No cloud requests, and no API key is read by this tool.
"""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from scipy.signal import resample_poly
from scipy.signal import correlate
import webrtcvad
from echo_control import PlaybackReference, SpeexAEC, SpeakerInput
from pcm_player import PCMPlayer
from voice_input import ChineseRecognizer, InputSession, TurnDetector, InputConfig, CALL_CONFIG, SpeechEnergyGate


def load(name):
    path = ROOT / f'voice-reference/prepared/{name}.wav'
    with wave.open(str(path), 'rb') as wav:
        rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), '<i2').astype(np.float64)
    import math
    divisor = math.gcd(rate, 16000)
    return resample_poly(samples, 16000 // divisor, rate // divisor), path


def pcm(samples):
    return np.clip(np.rint(samples), -32768, 32767).astype('<i2').tobytes()


def energy(samples):
    return float(np.mean(np.square(samples.astype(np.float64)))) + 1e-9


def synthetic(include_speech_gate=False):
    far, far_path = load('B')
    near, near_path = load('A')
    count = int(34 * 16000)
    far = np.resize(far, count) * .6
    far[-32000:] = 0
    echo = np.zeros(count)
    for delay, gain in ((800, .55), (1440, .15), (2240, .06)):
        echo[delay:] += gain * far[:-delay]
    wanted = np.zeros(count)
    near_start = 12 * 16000
    wanted[near_start:near_start + len(near)] = near * .65
    rng = np.random.default_rng(20261005)
    recorded = echo + wanted + rng.normal(0, 1.5, count)
    cleaned = []
    counts = dict(raw_onsets=0, cleaned_onsets=0, accepted_onsets=0)
    raw_detector, clean_detector = TurnDetector(), TurnDetector(InputConfig(onset_ms=240, minimum_ms=260))
    raw_vad, clean_vad = webrtcvad.Vad(2), webrtcvad.Vad(2)
    index = 0
    def reference(captured):
        return pcm(far[index:index+320]), True, 1
    processor = SpeakerInput(reference, lambda: index < count - 32000, lambda message: None)
    finals = []
    onset_times = []
    final_times = []
    confirmed_times = []
    recognizer = ChineseRecognizer()
    def onset():
        counts['accepted_onsets'] += 1
        onset_times.append(index/16000)
    def final(text, metrics):
        finals.append(text)
        final_times.append(index/16000)
    session = InputSession(recognizer, onset, lambda text: None, final, lambda text: None,
                           config=CALL_CONFIG if include_speech_gate else InputConfig(onset_ms=240, minimum_ms=260),
                           on_confirmed=lambda: confirmed_times.append(index/16000))
    gate = SpeechEnergyGate(CALL_CONFIG) if include_speech_gate else None
    if gate:
        # A real call opens and calibrates before its first speaker reply.
        for _ in range(CALL_CONFIG.calibration_ms // CALL_CONFIG.frame_ms):
            gate.process(bytes(640), False)
    for index in range(0, count, 320):
        raw = pcm(recorded[index:index+320])
        out, accepted = processor.process(raw, index/16000)
        cleaned.append(np.frombuffer(out, '<i2').copy())
        raw_events = raw_detector.push(raw, raw_vad.is_speech(raw, 16000), index/16000)
        counts['raw_onsets'] += sum(kind == 'start' for kind, _ in raw_events)
        clean_speech = clean_vad.is_speech(out, 16000) if accepted else False
        clean_events = clean_detector.push(out, clean_speech, index/16000)
        counts['cleaned_onsets'] += sum(kind == 'start' for kind, _ in clean_events)
        if accepted:
            if gate:
                was_ready = gate.ready
                clean_speech = gate.process(raw, clean_speech, session.detector.active,
                                          learn=not processor.audible())
                if not was_ready:
                    continue
            session.process(out, clean_speech, time.perf_counter())
        else:
            session.reset()
    metrics = processor.timings()
    processor.close()
    cleaned = np.concatenate(cleaned)
    pure = np.arange(count)
    mask = ((pure >= 3*16000) & (pure < near_start)) | ((pure >= near_start+len(near)+16000) & (pure < count-32000))
    reduction = 10*np.log10(energy(echo[mask])/energy(cleaned[mask]))
    target = wanted[near_start:near_start+len(near)]
    best = (-1., 0)
    for lag in range(0, 641, 16):
        result = cleaned[near_start+lag:near_start+lag+len(near)]
        correlation = float(np.corrcoef(target, result)[0, 1])
        if correlation > best[0]:
            best = (correlation, lag)
    result = cleaned[near_start+best[1]:near_start+best[1]+len(near)]
    near_gain_db = 10*np.log10(energy(result)/energy(target))
    output = dict(test='synthetic_delayed_reverberant_room', duration_seconds=34,
                  echo_taps_ms=[50,90,140], reduction_after_warmup_db=float(reduction),
                  near_speech_correlation=best[0], near_speech_delay_ms=best[1]/16,
                  near_speech_energy_change_db=float(near_gain_db), recognized_reference_text=finals,
                  onset_times=onset_times, final_times=final_times,
                  **counts, echo=metrics, reference_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (far_path,near_path)})
    if gate:
        output.update(speech_gate=gate.timings(), confirmed_onset_times=confirmed_times,
                      calibration_before_playback=True)
    output['passed'] = bool(reduction > 15 and best[0] > .7 and -6 < near_gain_db < 6
                            and 1 <= len(finals) <= 2 and any('神的想法' in text for text in finals)
                            and any('告诉你' in text for text in finals)
                            and len(onset_times) == 2 and all(12 <= value <= 22 for value in onset_times)
                            and all(not any(word in text for word in ('听我', '讨喜', '家伙')) for text in finals))
    if gate:
        output['passed'] = (output['passed'] and any('好吧' in text for text in finals)
                            and len(confirmed_times) == 2 and all(12 <= value <= 22 for value in confirmed_times))
    return output


def acoustic(device):
    import sounddevice as sd
    events = queue.Queue(maxsize=100)
    dropped = 0
    def callback(data, frames, timing, status):
        nonlocal dropped
        arrived = time.perf_counter()
        captured = min(arrived, arrived - max(0,timing.currentTime-timing.inputBufferAdcTime) + .02)
        try:
            events.put_nowait((bytes(data),captured,bool(status)))
        except queue.Full:
            dropped += 1
    reference = PlaybackReference()
    cancel = threading.Event()
    player = PCMPlayer(cancel, reference=reference)
    playing = threading.Event()
    def ref(captured):
        player.poll_reference()
        return reference.read(captured)
    processor = SpeakerInput(ref, playing.is_set, print)
    path = ROOT / 'voice-reference/prepared/B.wav'
    raw_detector, clean_detector = TurnDetector(), TurnDetector(InputConfig(onset_ms=240, minimum_ms=260))
    raw_vad, clean_vad = webrtcvad.Vad(2), webrtcvad.Vad(2)
    result = dict(test='physical_speaker_microphone_echo_only', raw_onsets=0, cleaned_onsets=0,
                  frames=0, reference_valid_frames=0, input_overflows=0, microphone_audio_saved=False,
                  transcripts_saved=False, output_device='waveOut default')
    energies = []
    raw_frames, reference_frames = [], []
    onset_times = []
    stream = sd.RawInputStream(device=device,samplerate=16000,channels=1,dtype='int16',blocksize=320,callback=callback)
    def play():
        try:
            playing.set()
            player.write(path)
            player.finish()
        finally:
            playing.clear()
    thread = threading.Thread(target=play,daemon=True)
    started = time.perf_counter()
    try:
        stream.start()
        # A quiet baseline is required before attributing onsets to echo.
        baseline_detector=TurnDetector()
        baseline_vad=webrtcvad.Vad(2)
        baseline_power=[]
        baseline_onsets=0
        baseline_until=time.perf_counter()+2
        while time.perf_counter()<baseline_until:
            try:
                raw,captured,overflow=events.get(timeout=.1)
            except queue.Empty:
                continue
            baseline_power.append(energy(np.frombuffer(raw,'<i2')))
            baseline_onsets+=sum(kind=='start' for kind,_ in baseline_detector.push(raw,baseline_vad.is_speech(raw,16000),captured))
        result['baseline_onsets']=baseline_onsets
        result['baseline_rms']=float(np.sqrt(np.mean(baseline_power))) if baseline_power else None
        if baseline_onsets:
            result['environment_warning']='Baseline contains speech-like background; echo attribution is inconclusive'
        while not events.empty(): events.get_nowait()
        started=time.perf_counter()
        thread.start()
        deadline = started + 18
        ended = None
        while time.perf_counter() < deadline:
            if not thread.is_alive():
                ended = ended or time.perf_counter()
                if time.perf_counter() - ended > 1.3:
                    break
            try:
                raw,captured,overflow = events.get(timeout=.1)
            except queue.Empty:
                continue
            if overflow:
                result['input_overflows'] += 1
            far, valid, _ = ref(captured)
            raw_frames.append(np.frombuffer(raw,'<i2').copy())
            reference_frames.append(np.frombuffer(far,'<i2').copy())
            out, admitted = processor.process(raw,captured)
            result['frames'] += 1
            result['reference_valid_frames'] += int(valid)
            result['raw_onsets'] += sum(kind=='start' for kind,_ in raw_detector.push(raw,raw_vad.is_speech(raw,16000),captured))
            speech = clean_vad.is_speech(out,16000) if admitted else False
            clean_events = clean_detector.push(out,speech,captured)
            new_onsets = sum(kind=='start' for kind,_ in clean_events)
            result['cleaned_onsets'] += new_onsets
            if new_onsets:
                onset_times.append(time.perf_counter()-started)
            if admitted and playing.is_set() and time.perf_counter()-started > 3:
                energies.append((energy(np.frombuffer(raw,'<i2')),energy(np.frombuffer(out,'<i2'))))
    finally:
        stream.abort(); stream.close()
        if thread.is_alive():
            cancel.set(); player.stop(); thread.join(3)
        processor.close()
        player.close()
    result.update(duration_seconds=time.perf_counter()-started,dropped_frames=dropped,
                  echo=processor.timings(),reference_error=reference.error,
                  playback_completed_frames=player.completed_frames,playback_submitted_frames=player.submitted_frames,
                  playback_drained=player.completed_frames==player.submitted_frames)
    if energies:
        powers = np.asarray(energies)
        result['microphone_energy_reduction_db'] = float(10*np.log10(powers[:,0].mean()/powers[:,1].mean()))
    if raw_frames:
        raw_capture = np.concatenate(raw_frames).astype(np.float64)
        mirrored = np.concatenate(reference_frames).astype(np.float64)
        dots = correlate(raw_capture,mirrored,method='fft')
        center = len(mirrored)-1
        selected = dots[max(0,center-8000):center+8001]
        peak = int(np.argmax(np.abs(selected))) + max(0,center-8000) - center
        result['reference_capture_peak_correlation'] = float(np.abs(selected).max()/np.sqrt(np.dot(raw_capture,raw_capture)*np.dot(mirrored,mirrored)+1))
        result['reference_capture_peak_lag_ms'] = peak/16
        original,_ = load('B')
        baseline = correlate(raw_capture,original,mode='valid',method='fft') if len(raw_capture)>=len(original) else np.array([0.])
        sums = np.concatenate(([0.],np.cumsum(raw_capture*raw_capture)))
        powers = sums[len(original):]-sums[:-len(original)]
        correlations = np.abs(baseline)/np.sqrt(np.maximum(powers*np.dot(original,original),1))
        result['original_capture_peak_correlation'] = float(correlations.max(initial=0))
        result['raw_capture_rms'] = float(np.sqrt(energy(raw_capture)))
        result['reference_rms'] = float(np.sqrt(energy(mirrored)))
    result['onset_times']=onset_times
    # Ambient speech, device selection and hardware enhancements affect this probe.
    # A useful physical result requires an audible echo in the raw capture.
    result['echo_only_no_false_onset'] = result['cleaned_onsets']==0
    result['echo_exposure_detected'] = result.get('original_capture_peak_correlation',0)>.3
    result['controlled_echo_probe_passed']=bool(result['echo_exposure_detected'] and result['echo_only_no_false_onset']
                                               and result.get('baseline_onsets')==0 and result['playback_drained']
                                               and not result['dropped_frames'] and not result['input_overflows'])
    return result


def main():
    os.environ.pop('ALI_API_Key',None)
    parser = argparse.ArgumentParser()
    parser.add_argument('--acoustic',action='store_true')
    parser.add_argument('--device',type=int)
    args=parser.parse_args()
    result=acoustic(args.device) if args.acoustic else synthetic()
    result.update(timezone='Asia/Shanghai',external_chat_requests=0,microphone_audio_saved=False)
    date=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')
    suffix='acoustic' if args.acoustic else 'synthetic'
    report=ROOT/f'runtime/echo-{suffix}-{date}.json'
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not args.acoustic and not result['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
