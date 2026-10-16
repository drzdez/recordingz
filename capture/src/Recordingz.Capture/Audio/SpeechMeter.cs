using System.Buffers.Binary;

namespace Recordingz.Capture.Audio;

/// <summary>RMS of 16-bit PCM over windows of Recorder.SpeechWindowS: a window above Recorder.SpeechRms is speech.
/// Used by the recording (LastMicSpeech) and the microphone test, so both judge a room the same way. Not thread
/// safe: one capture callback feeds it.</summary>
public sealed class SpeechMeter
{
    private double _sumSq;
    private long _samples;

    public int Windows { get; private set; }        // finished windows
    public int SpeechWindows { get; private set; }  // of them above SpeechRms
    public int MaxRms { get; private set; }         // loudest finished window

    /// <summary>Adds samples (samplesPerS counts every channel); true when this call finished a speech window.</summary>
    public bool Add(ReadOnlySpan<byte> pcm16, int samplesPerS)
    {
        for (int i = 0; i + 1 < pcm16.Length; i += 2)
        {
            double v = BinaryPrimitives.ReadInt16LittleEndian(pcm16.Slice(i, 2));
            _sumSq += v * v;
        }
        _samples += pcm16.Length / 2;
        if (_samples < samplesPerS * Recorder.SpeechWindowS)
            return false;
        double rms = Math.Sqrt(_sumSq / _samples);
        _sumSq = 0;
        _samples = 0;
        Windows++;
        MaxRms = Math.Max(MaxRms, (int)Math.Round(rms));
        if (rms <= Recorder.SpeechRms)
            return false;
        SpeechWindows++;
        return true;
    }
}
