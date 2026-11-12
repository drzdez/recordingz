using Recordingz.Capture.App;
using Recordingz.Capture.Core;
using Recordingz.Capture.Detection;

namespace Recordingz.Capture.Tests;

/// <summary>2026-11-11: after a Windows update the privacy registry showed no microphone use, three Teams meetings
/// went unrecorded and nothing said so. The capture sessions, Teams' own log and the warning.</summary>
public class CallSignalTests
{
    private static readonly Dictionary<int, (int Parent, string Name)> Tree = new()
    {
        [10] = (1, "ms-teams.exe"),
        [11] = (10, "msedgewebview2.exe"),  // Teams' audio in its WebView2
        [20] = (1, "whatsapp.exe"),
        [21] = (20, "msedgewebview2.exe"),
        [22] = (21, "msedgewebview2.exe"),
        [30] = (1, "zoom.exe"),
        [40] = (1, "recordingz.exe"),
        [41] = (40, "recordingz-capture.exe"),
        [50] = (1, "msedgewebview2.exe"),  // a WebView2 whose host is gone
    };

    private static HashSet<string> Idents(params int[] pids) =>
        MicUsers.SessionIdents(pids.Select(p => new CaptureSession("Mikrofon", p)),
                               pid => MicUsers.HostName(Tree, pid), pid => Recordingz.Capture.Audio.ProcessTree.IsTeams(Tree, pid),
                               new HashSet<int> { 41, 40 });

    [Fact]
    public void A_capture_session_names_the_app_that_records()
    {
        Assert.Equal(["ms-teams.exe"], Idents(11));
        Assert.Equal(["whatsapp.exe"], Idents(22));  // the host app, not its browser engine
        Assert.Equal(["zoom.exe"], Idents(30));
        Assert.Empty(Idents(41, 40, 0));  // this recorder and the tray that started it are no call
        Assert.Equal(["msedgewebview2.exe"], Idents(50));
    }

    [Fact]
    public void Teams_in_a_capture_session_is_a_call_even_without_the_registry()
    {
        var silent = new[] { new MicConsentEntry("MSTeams_8wekyb3d8bbwe", "MSTeams_8wekyb3d8bbwe", 5, 9) };  // stopped
        var state = MicUsers.Combine(silent, Idents(11, 30));
        Assert.True(state.Teams);
        Assert.Contains("zoom.exe", state.Users);
        Assert.False(MicUsers.Combine(silent, Idents(30)).Teams);
        // the registry alone still counts
        var holding = new[] { new MicConsentEntry("MSTeams_8wekyb3d8bbwe", "MSTeams_8wekyb3d8bbwe", 5, 0) };
        Assert.True(MicUsers.Combine(holding, new HashSet<string>()).Teams);
    }

    private const string Became = "2026-11-11T08:59:24.643437+01:00 0x000080ec <INFO> TeamsCallTracker: Call became active: {0} (total: 1)\n";
    private const string Ended = "2026-11-11T09:26:17.219455+01:00 0x000080ec <INFO> TeamsCallTracker: Call ended: {0} (remaining: 0)\n";
    private const string Noise = "2026-11-11T09:00:00.000000+01:00 0x000080ec <DBG>  CallInfo: NotifyCallEnded causeId: x\n";

    private static string TempDir()
    {
        var d = Path.Combine(Path.GetTempPath(), "recordingz-teamslog-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(d);
        return d;
    }

    [Fact]
    public void Teams_log_tracks_the_active_calls_across_polls_and_rotation()
    {
        var dir = TempDir();
        try
        {
            var first = Path.Combine(dir, "MSTeams_2026-11-11_08-32-54.00.log");
            File.WriteAllText(first, Noise + string.Format(Became, "aaa") + string.Format(Ended, "aaa") + Noise);
            File.WriteAllText(Path.Combine(dir, "MSTeamsBackgroundEcs_2026-11-11_13-02-11.1124.log"), string.Format(Became, "zzz"));
            var log = new TeamsCallLog(dir);
            log.Poll();
            Assert.Null(log.Latest);  // the call of the morning is over; the background log is not read

            // a line cut in the middle is finished by the next poll
            var line = string.Format(Became, "bbb");
            File.AppendAllText(first, line[..40]);
            log.Poll();
            Assert.Null(log.Latest);
            File.AppendAllText(first, line[40..]);
            log.Poll();
            Assert.Equal("bbb", log.Latest);

            // Teams goes on in a new file (a fresh one looks empty to the directory listing): the call stays active,
            // the next call is joined straight after the first one ends
            var second = Path.Combine(dir, "MSTeams_2026-11-11_10-23-52.01.log");
            File.WriteAllText(second, string.Format(Ended, "bbb") + string.Format(Became, "ccc"));
            log.Poll();
            Assert.Equal(["ccc"], log.ActiveCalls);

            log.Reset();  // Teams quit
            Assert.Null(log.Latest);
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public void Teams_log_missing_is_no_call()
    {
        var log = new TeamsCallLog(Path.Combine(Path.GetTempPath(), "recordingz-no-such-dir-" + Guid.NewGuid().ToString("N")));
        log.Poll();
        Assert.Null(log.Latest);
    }

    [Fact]
    public void The_next_call_in_the_Teams_log_ends_the_recorded_one()
    {
        Assert.True(AppLogic.NextTeamsCall("aaa", ["bbb"]));
        Assert.False(AppLogic.NextTeamsCall("aaa", ["aaa"]));
        Assert.False(AppLogic.NextTeamsCall("aaa", ["aaa", "bbb"]));  // the recorded call still goes on
        Assert.False(AppLogic.NextTeamsCall("aaa", []));              // ended, nothing new: the microphone decides
        Assert.False(AppLogic.NextTeamsCall(null, ["bbb"]));          // the recorded call was never in the log
    }

    [Fact]
    public void A_call_nobody_records_is_warned_about_once_after_a_while()
    {
        double? since = null;
        Assert.False(AppLogic.UnrecordedCall("c1", false, false, null, ref since, 100));
        Assert.False(AppLogic.UnrecordedCall("c1", false, false, null, ref since, 100 + AppLogic.UnrecordedWarnS - 1));
        Assert.True(AppLogic.UnrecordedCall("c1", false, false, null, ref since, 100 + AppLogic.UnrecordedWarnS));
        // warned about already, recording, discarded on purpose, or no call: nothing
        Assert.False(AppLogic.UnrecordedCall("c1", false, false, "c1", ref since, 200));
        Assert.Null(since);
        since = 100;
        Assert.False(AppLogic.UnrecordedCall("c1", true, false, null, ref since, 200));
        Assert.Null(since);
        Assert.False(AppLogic.UnrecordedCall("c1", false, true, null, ref since, 200));
        Assert.False(AppLogic.UnrecordedCall(null, false, false, null, ref since, 200));
        // the next call is warned about too
        Assert.False(AppLogic.UnrecordedCall("c2", false, false, "c1", ref since, 300));
        Assert.True(AppLogic.UnrecordedCall("c2", false, false, "c1", ref since, 300 + AppLogic.UnrecordedWarnS));
    }
}
