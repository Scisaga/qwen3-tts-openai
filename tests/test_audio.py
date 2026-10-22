import numpy as np
import pytest
import soundfile as sf

from qwen3_tts_openai.audio_processing import AudioProcessor
from qwen3_tts_openai.errors import ServiceError


def dominant_frequency(path):
    data, sr = sf.read(path)
    data = data[len(data)//4:len(data)*3//4]
    return np.fft.rfftfreq(len(data),1/sr)[np.argmax(abs(np.fft.rfft(data)))]


def test_real_rubberband_tempo_pitch_gain_and_mp3(settings, tmp_path):
    audio = AudioProcessor(settings)
    assert audio.self_check()["state"] == "ready"
    source, target, mp3 = (tmp_path/n for n in ("input.wav","processed.wav","output.mp3"))
    sf.write(source, .1*np.sin(np.arange(48000)*2*np.pi*440/24000), 24000, subtype="PCM_16")
    audio.process(source,target,0.8,12,-6)
    data,sr=sf.read(target)
    assert len(data)/sr == pytest.approx(2.5, abs=.05)
    assert dominant_frequency(target) == pytest.approx(880, abs=5)
    # Formant preservation changes a pure tone's amplitude; compare gain against the same stretch.
    unscaled=tmp_path/"unscaled.wav"
    audio.process(source,unscaled,.8,12,0)
    reference,_=sf.read(unscaled)
    assert np.sqrt(np.mean(data**2))/np.sqrt(np.mean(reference**2)) == pytest.approx(10**(-6/20),rel=.02)
    audio.encode(target,mp3)
    info=audio.encoded_info(mp3)
    assert info["sample_rate"] == 24000 and info["bit_rate"] == 128000
    assert info["decoded_frames"] == audio.frames(target)
    assert info["container_duration_seconds"] >= info["decoded_duration_seconds"]


def test_real_silence_and_timeline_after_processing(settings, sample, tmp_path):
    audio=AudioProcessor(settings)
    processed=tmp_path/"unit.wav"
    audio.process(sample,processed,2,0,0)
    timeline=audio.assemble([{"kind":"bookmark","mark":"start","page":1},{"kind":"speech","unit_id":"one","page":1},{"kind":"silence","pause_ms":200,"page":1}],{"one":processed},tmp_path/"full.wav",lambda:None)
    assert timeline["events"][0]["end_frame"] == audio.frames(processed)
    assert timeline["events"][1]["end_frame"]-timeline["events"][1]["start_frame"] == 4800
    samples,_=sf.read(tmp_path/"full.wav")
    assert np.max(np.abs(samples[-4800:])) == 0
    assert not timeline["word_alignment"]


def test_ffmpeg_failure_real_exit_and_cleanup(settings, sample, tmp_path):
    audio=AudioProcessor(settings.model_copy(update={"ffmpeg_bin":"/bin/false"}))
    with pytest.raises(ServiceError) as caught:
        audio.process(sample,tmp_path/"failed.wav",1,0,0)
    assert caught.value.code == "ffmpeg_failed"
    assert not (tmp_path/"failed.wav").exists()
    with pytest.raises(ServiceError) as caught:
        audio.self_check()
    assert audio.status["state"] == "failed"


def test_reference_native_rate_and_duration_limits(settings, tmp_path):
    original,processed=tmp_path/"stereo.wav",tmp_path/"reference.wav"
    samples=.1*np.sin(np.arange(44100*2)*2*np.pi*220/44100)
    sf.write(original,np.column_stack([samples,samples]),44100)
    metadata=AudioProcessor(settings).validate_reference(original,processed)
    assert metadata["sample_rate"] == sf.info(processed).samplerate == 44100
    assert sf.info(original).channels == 2 and sf.info(processed).channels == 1
    sf.write(original,np.zeros(24000*31),24000)
    with pytest.raises(ServiceError) as caught:
        AudioProcessor(settings).validate_reference(original,processed)
    assert caught.value.code == "reference_duration"


def test_float_raw_audio_can_be_reprocessed_without_prior_clipping(settings,tmp_path):
    source,target=tmp_path/"float-raw.wav",tmp_path/"lower-gain.wav"
    samples=1.2*np.sin(np.arange(24000)*2*np.pi*440/24000)
    sf.write(source,samples,24000,subtype="FLOAT")
    AudioProcessor(settings).process(source,target,1,0,-6)
    raw,_=sf.read(source)
    processed,_=sf.read(target)
    assert np.max(raw)>1.19
    assert np.max(processed)==pytest.approx(1.2*10**(-6/20),abs=.001)


@pytest.mark.parametrize("sample_rate,bitrate",[(24000,"160k"),(44100,"192k"),(32000,"256k")])
def test_encoder_honors_actual_sample_rate_and_bitrate(settings,sample,tmp_path,sample_rate,bitrate):
    audio=AudioProcessor(settings.model_copy(update={"work_sample_rate":sample_rate,"mp3_bitrate":bitrate}))
    wav,mp3=tmp_path/"processed.wav",tmp_path/"encoded.mp3"
    audio.process(sample,wav,1,0,0)
    audio.encode(wav,mp3)
    measured=audio.encoded_info(mp3)
    assert measured["sample_rate"]==sample_rate
    assert measured["bit_rate"]==int(bitrate[:-1])*1000
