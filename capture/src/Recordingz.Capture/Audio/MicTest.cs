using NAudio.CoreAudioApi;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.Audio;

/// <summary>Settings page "test the microphone": open an input the way a recording would and report how loud
/// it is (port of test_input). The verdict is the recording's own: speech is a half second whose RMS passes
/// Recorder.SpeechRms (SpeechMeter); peaks alone call room noise with an odd click "loud" (2026-10-12).</summary>
public static class MicTest
{
    public const double Seconds = 3;

    public sealed record Result(string Device, int Peak, int MaxRms, int SpeechWindows, string? Error = null)
    {
        public bool Speech => SpeechWindows > 0;

        /// <summary>What the tray and the settings page say.</summary>
        public string Message => Error ?? $"{Device}: " + (Speech
            ? $"slyší řeč (hlasitost {MaxRms}, řeč od {Recorder.SpeechRms})"
            : Peak > Recorder.AliveLevel
                ? $"jen šum, žádná řeč (hlasitost {MaxRms}, řeč od {Recorder.SpeechRms}) – mluvte během testu, " +
                  "u stolu dál od mikrofonu nebude slyšet"
                : "TICHO – mikrofon nic nedodává (vypnutý, nebo ho drží jiná aplikace)");
    }

    /// <summary>Judges 16-bit PCM the way Run does (samplesPerS counts every channel).</summary>
    public static Result Measure(string device, IEnumerable<byte[]> pcm16, int samplesPerS)
    {
        var meter = new SpeechMeter();
        int peak = 0;
        foreach (var b in pcm16)
        {
            peak = Math.Max(peak, Pcm.Peak16(b));
            meter.Add(b, samplesPerS);
        }
        return new Result(device, peak, meter.MaxRms, meter.SpeechWindows);
    }

    public static Result Run(string namePart, double seconds = Seconds)
    {
        namePart ??= "";
        try
        {
            using var en = new MMDeviceEnumerator();
            using var dev = Devices.FindInputEndpoint(en, namePart);
            if (dev == null)
                return new Result("", 0, 0, 0, $"zařízení „{(namePart.Length > 0 ? namePart : "výchozí vstup")}“ není k dispozici");
            string name = dev.FriendlyName;
            var capture = new WasapiCapture(dev);
            SampleFormat fmt;
            int outChannels;
            try
            {
                fmt = SampleFormat.Of(capture.WaveFormat);
                outChannels = Math.Clamp(capture.WaveFormat.Channels, 1, 2);
            }
            catch
            {
                capture.Dispose();
                throw;
            }
            int samplesPerS = capture.WaveFormat.SampleRate * outChannels;
            var buffers = new List<byte[]>();
            Exception? failed = null;
            var gate = new object();
            capture.DataAvailable += (_, e) =>
            {
                if (e.BytesRecorded <= 0)
                    return;
                var pcm = Pcm.ToPcm16(e.Buffer.AsSpan(0, e.BytesRecorded), fmt, outChannels);
                lock (gate)
                    buffers.Add(pcm);
            };
            capture.RecordingStopped += (_, e) =>
            {
                lock (gate)
                    failed ??= e.Exception;
            };
            try
            {
                capture.StartRecording();
                Thread.Sleep(TimeSpan.FromSeconds(Math.Max(0.1, seconds)));
                capture.StopRecording();
            }
            finally
            {
                capture.Dispose();  // joins the capture thread, so the buffers are final
            }
            lock (gate)
            {
                if (failed != null)
                    throw failed;
                var r = Measure(name, buffers, samplesPerS);
                Log.Info($"microphone test: {name} peak {r.Peak} rms {r.MaxRms} speech windows {r.SpeechWindows}");
                return r;
            }
        }
        catch (Exception e)
        {
            Log.Error($"microphone test: {e}");
            return new Result("", 0, 0, 0, $"test selhal: {e.Message}");
        }
    }
}
