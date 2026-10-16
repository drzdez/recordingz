using Recordingz.Capture.Core;

namespace Recordingz.Capture.Recording;

/// <summary>What the monitor loop asks the user when an on-site recording has been silent for a long time; the
/// answer goes back to <see cref="OnsiteSilence.Answer"/>. SilentSinceS: seconds into the recording when the
/// silence started (for the log).</summary>
public sealed record SilencePrompt(int Generation, double SilentSinceS, double SilentMin);

/// <summary>
/// An on-site recording has no call whose end could stop it (2026-10-12: a forgotten Stop recorded 4 hours of
/// room noise up to max_duration, the microphone heard nobody the whole time). Pure decisions, like the
/// <see cref="Watchdog"/>: speech comes from <see cref="IAudioSource.LastMicSpeech"/>, time from
/// <see cref="IClock"/>. The App calls <see cref="Tick"/> once per tray loop tick, shows the returned prompt
/// outside the loop thread and hands the answer to <see cref="Answer"/>; <see cref="Reset"/> when a new recording
/// starts. Live, manual and playback recordings are left alone (they are not MicOnly).
/// </summary>
public sealed class OnsiteSilence
{
    public const double NoSpeechWarnS = 180;   // nobody spoke this long after the start: the mic is probably wrong
    public const int DefaultSilenceMin = 10;   // [capture] onsite_silence_min
    public const int AskTimeoutS = 300;        // the "still running?" box closes after this and the recording stops

    // A box that comes back unanswered this much before its timeout never showed: keep recording.
    private const double BoxFailedS = 5;

    private readonly IClock _clock;
    private readonly INotifier _notifier;
    private readonly object _lock = new();  // Tick runs on the loop thread, Answer on the dialog's
    private int _generation;
    private bool _noSpeechWarned;
    private double _armedAt;   // silence counts from here at the earliest (the last "Ano")
    private double? _askedAt;  // a question is open

    public OnsiteSilence(IClock clock, INotifier notifier)
    {
        _clock = clock;
        _notifier = notifier;
        Reset();
    }

    /// <summary>A new recording starts: forget the warning, the open question and the last "Ano".</summary>
    public void Reset()
    {
        lock (_lock)
        {
            _generation++;
            _noSpeechWarned = false;
            _armedAt = double.NegativeInfinity;
            _askedAt = null;
        }
    }

    /// <summary>
    /// Warn once when nobody has spoken <see cref="NoSpeechWarnS"/> after the start (unless the watchdog already
    /// says the microphone is dead), and return a prompt when there has been no speech for
    /// <paramref name="silenceMin"/> minutes (0 = never ask). Null = nothing to ask now.
    /// </summary>
    /// <param name="rec">The running recorder.</param>
    /// <param name="elapsedS">Seconds since the recording started.</param>
    /// <param name="silenceMin">[capture] onsite_silence_min.</param>
    /// <param name="micWarned">The watchdog already alarms about this microphone.</param>
    public SilencePrompt? Tick(IAudioSource rec, double elapsedS, int silenceMin, bool micWarned)
    {
        if (!rec.MicOnly)
            return null;
        double now = _clock.Seconds;
        double start = now - elapsedS;
        double? speech = rec.LastMicSpeech;
        lock (_lock)
        {
            if (!_noSpeechWarned && speech is null && elapsedS >= NoSpeechWarnS)
            {
                _noSpeechWarned = true;
                Log.Warn($"on-site: no speech on the microphone in the first {elapsedS / 60:0} min");
                if (!micWarned)
                {
                    _notifier.Notify("Nahrávka na místě: mikrofon zatím nic neslyší – je notebook u stolu "
                                     + "a mikrofon zapnutý?");
                    _notifier.Beep();
                }
            }
            if (silenceMin <= 0 || _askedAt is not null)
                return null;
            double since = Math.Max(speech ?? start, _armedAt);
            if (now - since < silenceMin * 60.0)
                return null;
            _askedAt = now;
            double offset = Math.Max(0.0, since - start);
            Log.Info($"on-site: no speech for {(now - since) / 60:0} min (silence_since {offset:0} s into the "
                     + "recording), asking whether the meeting still runs");
            return new SilencePrompt(_generation, offset, (now - since) / 60);
        }
    }

    /// <summary>The user's answer to a <see cref="SilencePrompt"/>: true = "Ano" (keep recording, the silence
    /// counts anew), false = "Ne", null = nobody answered. Returns true when the recording should stop
    /// (stop_reason "silence"); the trailing silence stays in the files. An answer to the prompt of an earlier
    /// recording is ignored.</summary>
    public bool Answer(SilencePrompt prompt, bool? choice)
    {
        lock (_lock)
        {
            if (prompt.Generation != _generation || _askedAt is not { } askedAt)
                return false;
            double now = _clock.Seconds;
            _askedAt = null;
            if (choice is null && now - askedAt < AskTimeoutS - BoxFailedS)
            {
                Log.Warn("on-site: the \"still running?\" box did not show, recording on");
                _armedAt = now;
                return false;
            }
            if (choice == true)
            {
                Log.Info("on-site: the meeting still runs, recording on");
                _armedAt = now;
                return false;
            }
            Log.Info($"on-site: {(choice == false ? "the meeting is over" : "no answer")}, stopping "
                     + $"(silence_since {prompt.SilentSinceS:0} s)");
            return true;
        }
    }

    /// <summary>The text of the "still running?" box.</summary>
    public static string PromptText(double silentMin) =>
        $"Nahrávka na místě: už {silentMin:0} min nikdo nemluví.\n\nSchůzka ještě běží?\n"
        + $"Ano = pokračovat v nahrávání, Ne = ukončit a nahrávku uložit "
        + $"(bez odpovědi se za {AskTimeoutS / 60} min ukončí a uloží sama).";
}
