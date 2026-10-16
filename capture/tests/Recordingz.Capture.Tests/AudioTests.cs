using System.Buffers.Binary;
using Recordingz.Capture.Audio;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.Tests;

public class AudioTests
{
    private static readonly Dictionary<int, string> Names = new()
    {
        [100] = "ms-teams.exe", [200] = "zoom.exe", [300] = "chrome.exe", [400] = "voiceassistant.exe",
    };

    private static string? Call(params CaptureSession[] sessions) =>
        Devices.CallInputDevice(sessions,
                                pid => Names.GetValueOrDefault(pid, ""),
                                pid => Names.GetValueOrDefault(pid) == "ms-teams.exe");

    [Fact]
    public void The_call_app_microphone_is_found_whichever_app_it_is()
    {
        Assert.Equal("Sluchátka (WH-1000XM6)",
                     Call(new CaptureSession("Pole mikrofonu", 400), new CaptureSession("Sluchátka (WH-1000XM6)", 100), new CaptureSession("Mikrofon (BT-W5)", 200)));
        // a known call app beats an unknown one
        Assert.Equal("Mikrofon (BT-W5)", Call(new CaptureSession("Pole mikrofonu", 400), new CaptureSession("Mikrofon (BT-W5)", 200)));
        Assert.Equal("Headset (Meet v prohlížeči)", Call(new CaptureSession("Headset (Meet v prohlížeči)", 300)));
        // our own recording is not a call
        Assert.Null(Call(new CaptureSession("Mikrofon (BT-W5)", Environment.ProcessId)));
        // system sessions are not a call
        Assert.Null(Call(new CaptureSession("Mikrofon (BT-W5)", 0)));
    }

    [Fact]
    public void Call_apps_rank_in_their_listed_order_and_unknown_apps_still_count()
    {
        // zoom.exe is before chrome.exe in CALL_APPS, whatever order the sessions come in
        Assert.Equal("Zoom mic", Call(new CaptureSession("Chrome mic", 300), new CaptureSession("Zoom mic", 200)));
        Assert.Equal("Asistent", Call(new CaptureSession("Asistent", 400)));
        Assert.Null(Call());
    }

    [Fact]
    public void Teams_is_recognised_through_a_helper_process_parent()
    {
        var tree = new Dictionary<int, (int Parent, string Name)>
        {
            [10] = (1, "ms-teams.exe"),
            [11] = (10, "msedgewebview2.exe"),
            [12] = (11, "msedgewebview2.exe"),
            [20] = (1, "explorer.exe"),
            [21] = (20, "zoom.exe"),
            [30] = (31, "a.exe"), [31] = (32, "b.exe"), [32] = (33, "c.exe"), [33] = (34, "d.exe"),
            [34] = (1, "ms-teams.exe"),
        };
        Assert.True(ProcessTree.IsTeams(tree, 10));
        Assert.True(ProcessTree.IsTeams(tree, 12));
        Assert.False(ProcessTree.IsTeams(tree, 21));
        Assert.False(ProcessTree.IsTeams(tree, 999));
        Assert.False(ProcessTree.IsTeams(tree, 30));  // Teams five levels up is too far (prototype: 4)
    }

    [Theory]
    [InlineData(2, "zakázané ve Windows")]
    [InlineData(0x10000001, "zakázané ve Windows")]
    [InlineData(4, "není přítomné")]
    [InlineData(8, "odpojené")]
    [InlineData(1, "aktivní")]
    [InlineData(64, "stav 64")]
    public void Unavailable_reasons_are_in_czech(int state, string reason) =>
        Assert.Equal(reason, Devices.StateReason(state));

    private static byte[] Int16(params short[] samples)
    {
        var b = new byte[samples.Length * 2];
        for (int i = 0; i < samples.Length; i++)
            BinaryPrimitives.WriteInt16LittleEndian(b.AsSpan(i * 2), samples[i]);
        return b;
    }

    private static short[] Samples(byte[] pcm16)
    {
        var s = new short[pcm16.Length / 2];
        for (int i = 0; i < s.Length; i++)
            s[i] = BinaryPrimitives.ReadInt16LittleEndian(pcm16.AsSpan(i * 2));
        return s;
    }

    [Fact]
    public void Peak_is_the_largest_absolute_sample()
    {
        Assert.Equal(0, Pcm.Peak16(Int16(0, 0, 0)));
        Assert.Equal(301, Pcm.Peak16(Int16(12, -301, 300)));
        Assert.Equal(32768, Pcm.Peak16(Int16(short.MinValue, 5)));
        Assert.Equal(0, Pcm.Peak16(ReadOnlySpan<byte>.Empty));
        // the thresholds the watchdog relies on
        Assert.True(Pcm.Peak16(Int16(9)) > Recorder.AliveLevel);
        Assert.False(Pcm.Peak16(Int16(300)) > Recorder.SilenceLevel);
    }

    [Fact]
    public void Float_stereo_becomes_16_bit_with_the_same_channels()
    {
        var src = new byte[4 * 4];
        float[] v = [0.5f, -1.0f, 2.0f, float.NaN];  // 2 frames x 2 channels; out of range is clipped
        for (int i = 0; i < v.Length; i++)
            BinaryPrimitives.WriteSingleLittleEndian(src.AsSpan(i * 4), v[i]);
        var pcm = Pcm.ToPcm16(src, new SampleFormat(true, 4, 2), 2);
        Assert.Equal(new short[] { 16384, -32767, 32767, 0 }, Samples(pcm));
    }

    [Fact]
    public void Surround_keeps_the_front_pair_and_mono_is_mono()
    {
        // 1 frame of 6 channels, 16-bit
        var six = Int16(1, 2, 3, 4, 5, 6);
        Assert.Equal(new short[] { 1, 2 }, Samples(Pcm.ToPcm16(six, new SampleFormat(false, 2, 6), 2)));
        var mono = Int16(7, -7);
        Assert.Equal(new short[] { 7, -7 }, Samples(Pcm.ToPcm16(mono, new SampleFormat(false, 2, 1), 1)));
    }

    [Fact]
    public void Integer_formats_keep_their_top_16_bits()
    {
        // 24-bit 0x123456 -> 0x1234; 32-bit 0x7FFF0000 -> 0x7FFF; 8-bit 0 (unsigned) -> -32768
        Assert.Equal(new short[] { 0x1234 }, Samples(Pcm.ToPcm16(new byte[] { 0x56, 0x34, 0x12 }, new SampleFormat(false, 3, 1), 1)));
        var i32 = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(i32, 0x7FFF0000);
        Assert.Equal(new short[] { 0x7FFF }, Samples(Pcm.ToPcm16(i32, new SampleFormat(false, 4, 1), 1)));
        Assert.Equal(new short[] { -32768 }, Samples(Pcm.ToPcm16(new byte[] { 0 }, new SampleFormat(false, 1, 1), 1)));
        // a partial frame at the end is dropped
        Assert.Empty(Pcm.ToPcm16(new byte[] { 1, 2, 3 }, new SampleFormat(false, 2, 2), 2));
    }

    [Fact]
    public void Sample_format_is_read_from_the_wave_format()
    {
        var f = SampleFormat.Of(NAudio.Wave.WaveFormat.CreateIeeeFloatWaveFormat(48000, 2));
        Assert.Equal(new SampleFormat(true, 4, 2), f);
        var ext = SampleFormat.Of(new NAudio.Wave.WaveFormatExtensible(48000, 32, 2));  // NAudio: 32 bit = float
        Assert.Equal(new SampleFormat(true, 4, 2), ext);
        Assert.Equal(new SampleFormat(false, 2, 1), SampleFormat.Of(new NAudio.Wave.WaveFormat(16000, 16, 1)));
    }

    [Fact]
    public void A_new_recorder_starts_with_fresh_watchdog_values()
    {
        var clock = new FakeClock(1000);
        using var rec = new Recorder(@"C:\nowhere\x", withMic: true, micOnly: false, micName: "", clock);
        Assert.Equal(1000, rec.LastData);
        Assert.Equal(1000, rec.LastLoud);
        Assert.Equal(1000, rec.LastMicAlive);
        Assert.Equal(0, rec.LastMicLoud);
        Assert.Null(rec.LastMicData);
        Assert.Null(rec.LastMicSpeech);
        Assert.False(rec.HeardSys);
        Assert.False(rec.HeardMic);
        Assert.Empty(rec.Tracks);
        Assert.Equal(0, rec.Reopens);
    }

    /// <summary>A 10 ms buffer of 48 kHz stereo: Gaussian noise of the given RMS, optionally with one click.</summary>
    private static byte[] Buffer10ms(Random rnd, double rms, short click = 0)
    {
        var b = new byte[480 * 2 * 2];
        for (int i = 0; i < b.Length / 2; i++)
        {
            double g = Math.Sqrt(-2 * Math.Log(1 - rnd.NextDouble())) * Math.Cos(2 * Math.PI * rnd.NextDouble());
            BinaryPrimitives.WriteInt16LittleEndian(b.AsSpan(i * 2), (short)Math.Clamp(g * rms, -32768, 32767));
        }
        if (click != 0)
            BinaryPrimitives.WriteInt16LittleEndian(b.AsSpan(100), click);
        return b;
    }

    [Fact]
    public void Room_noise_with_clicks_is_not_speech_but_a_voice_is()
    {
        // 2026-10-12: room noise at RMS ~-55 dBFS (58) whose peaks pass SilenceLevel now and then (-26 dBFS = 1650)
        var clock = new FakeClock(1000);
        using var rec = new Recorder(@"C:\nowhere\x", withMic: true, micOnly: true, micName: "", clock);
        var rnd = new Random(12);
        for (int i = 0; i < 6000; i++)  // a minute
        {
            var buf = Buffer10ms(rnd, 58, click: i % 100 == 0 ? (short)1650 : (short)0);
            Assert.True(i % 100 != 0 || Pcm.Peak16(buf) > Recorder.SilenceLevel);  // the peak would call it loud
            rec.MeasureSpeech(buf, 48000 * 2, clock.Seconds += 0.01);
        }
        Assert.Null(rec.LastMicSpeech);

        // a voice near the laptop: ~-35 dBFS RMS (580), half a second of it
        for (int i = 0; i < 50; i++)
            rec.MeasureSpeech(Buffer10ms(rnd, 580), 48000 * 2, clock.Seconds += 0.01);
        Assert.Equal(clock.Seconds, rec.LastMicSpeech);
    }

    [Fact]
    public void Microphone_test_judges_speech_by_rms_not_peaks()
    {
        var rnd = new Random(7);
        // the room of 2026-10-12: noise with a click a second; the old test said "slyší" by the peak
        var room = Enumerable.Range(0, 300).Select(i => Buffer10ms(rnd, 58, click: i % 100 == 0 ? (short)1650 : (short)0));
        var r = MicTest.Measure("Pole", room.ToList(), 48000 * 2);
        Assert.True(r.Peak > Recorder.SilenceLevel);
        Assert.False(r.Speech);
        Assert.InRange(r.MaxRms, 50, 70);
        Assert.StartsWith("Pole: jen šum, žádná řeč", r.Message);

        var voice = Enumerable.Range(0, 300).Select(i => Buffer10ms(rnd, i < 100 ? 58 : 580)).ToList();
        r = MicTest.Measure("Pole", voice, 48000 * 2);
        Assert.True(r.Speech);
        Assert.Equal(4, r.SpeechWindows);
        Assert.StartsWith("Pole: slyší řeč", r.Message);

        r = MicTest.Measure("Dongle", [new byte[48000 * 2 * 2 * 3]], 48000 * 2);
        Assert.Equal(0, r.MaxRms);
        Assert.StartsWith("Dongle: TICHO", r.Message);

        Assert.Equal("test selhal: x", new MicTest.Result("", 0, 0, 0, "test selhal: x").Message);
    }

    [Fact]
    public void Speech_meter_counts_whole_windows_only()
    {
        var m = new SpeechMeter();
        var loud = new byte[4800 * 2];  // 0.1 s of mono 48 kHz
        for (int i = 0; i < loud.Length; i += 2)
            BinaryPrimitives.WriteInt16LittleEndian(loud.AsSpan(i), (short)(i % 4 == 0 ? 1000 : -1000));
        for (int i = 0; i < 4; i++)
            Assert.False(m.Add(loud, 48000));
        Assert.Equal(0, m.Windows);
        Assert.True(m.Add(loud, 48000));
        Assert.Equal((1, 1, 1000), (m.Windows, m.SpeechWindows, m.MaxRms));
    }

    private sealed class FakeClock(double seconds) : IClock
    {
        public DateTime Now { get; set; } = new(2026, 9, 29, 14, 0, 0);
        public double Seconds { get; set; } = seconds;
    }
}
