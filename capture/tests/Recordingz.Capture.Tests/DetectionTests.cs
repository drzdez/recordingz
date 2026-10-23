using Recordingz.Capture.Detection;

namespace Recordingz.Capture.Tests;

public class DetectionTests
{
    [Fact]
    public void PrejoinDetection()
    {
        // The join dialog holds the microphone but is not a call (2026-09-21: 6.5 min of the dialog recorded).
        const string join = "Připojení ke schůzce | Team week plan | Microsoft Teams";
        Assert.True(CallDetector.PrejoinOnly([join, "Calendar | Microsoft Teams", "Chat | Jan Novák | Microsoft Teams"]));
        Assert.False(CallDetector.PrejoinOnly([join, "Kompaktní zobrazení schůzky | Team week plan | Microsoft Teams"]));
        Assert.False(CallDetector.PrejoinOnly([join, "Ovládací panel sdílení | Microsoft Teams"]));
        Assert.False(CallDetector.PrejoinOnly(["Team standup | Microsoft Teams"]));
        Assert.False(CallDetector.PrejoinOnly(["Schůzka s: Jan Novák | Microsoft Teams"]));
        Assert.False(CallDetector.PrejoinOnly([]));
    }

    [Fact]
    public void The_join_dialog_of_another_meeting_ends_the_recorded_call()
    {
        // 2026-10-23: from a stand-up straight to the next meeting, Teams kept the microphone and the stand-up
        // recorded on
        string[] seen = ["Archi standup | Microsoft Teams", "Calendar | Microsoft Teams", "Archi standup"];
        const string next = "Připojení ke schůzce | Software type / Licensing | Microsoft Teams";
        Assert.Equal("Software type / Licensing", CallDetector.NextMeetingJoin([next, "Calendar | Microsoft Teams"], seen));
        Assert.Equal("Software type / Licensing",
                     CallDetector.NextMeetingJoin(["Archi standup | Microsoft Teams", next], seen), ignoreCase: true);
        // the recorded meeting's own join dialog, still listed when the recording starts, is not another meeting
        Assert.Null(CallDetector.NextMeetingJoin(["Připojení ke schůzce | ARCHI STANDUP | Microsoft Teams"], seen));
        Assert.Null(CallDetector.NextMeetingJoin(["Připojení ke schůzce | Microsoft Teams"], seen));  // no subject
        Assert.Null(CallDetector.NextMeetingJoin(["Software type / Licensing | Microsoft Teams"], seen));
        Assert.Null(CallDetector.NextMeetingJoin([next], [.. seen, next]));  // seen before: it was there all along
    }

    [Fact]
    public void After_a_call_the_next_meeting_starts_once_joined_even_with_the_old_window_open()
    {
        const string join = "Připojení ke schůzce | Software type | Microsoft Teams";
        string[] joining = [join, "Archi standup | Microsoft Teams"];
        Assert.False(CallDetector.PrejoinOnly(joining), "the old meeting window looks like a joined call");
        Assert.True(CallDetector.StillJoining(joining, "Software type"));
        Assert.False(CallDetector.StillJoining(joining, null));
        Assert.False(CallDetector.StillJoining([join, "Software type | Microsoft Teams"], "Software type"));
        Assert.False(CallDetector.StillJoining(["Archi standup | Microsoft Teams"], "Software type"), "join dialog closed");
    }

    private static readonly Dictionary<string, string[]> BrowserTitles = new()
    {
        ["chrome.exe"] = ["Meet – abc-defg-hij - Google Chrome", "Inbox - Gmail - Google Chrome"],
        ["msedge.exe"] = ["YouTube - Microsoft​ Edge"],
    };

    private static List<string> TitlesOf(IEnumerable<string> exes) =>
        exes.SelectMany(e => BrowserTitles.TryGetValue(e, out var t) ? t : Array.Empty<string>()).ToList();

    private static HashSet<string> Set(params string[] s) => new(s);

    [Fact]
    public void CallsInOtherAppsAreRecognised()
    {
        Assert.Equal("Zoom", CallDetector.OtherCall(Set("zoom.exe"), TitlesOf)?.App);
        Assert.Equal("WhatsApp", CallDetector.OtherCall(Set("5319275a.whatsappdesktop_cv1g1gvanyjgm"), TitlesOf)?.App);
        var web = CallDetector.OtherCall(Set("chrome.exe"), TitlesOf);
        Assert.NotNull(web);
        Assert.Equal("Meet – abc-defg-hij", web.Title);
        Assert.Equal("chrome.exe", web.Id);
        Assert.Equal("Chrome (Meet – abc-defg-hij)", web.App);
        // a browser without a meeting is not a call (dictation, search)
        Assert.Null(CallDetector.OtherCall(Set("msedge.exe"), TitlesOf));
        // another recorder is not a call
        Assert.Null(CallDetector.OtherCall(Set("read_ai_desktop.exe"), TitlesOf));
        // Teams has its own path
        Assert.Null(CallDetector.OtherCall(Set("msteams_8wekyb3d8bbwe", "zoom.exe"), TitlesOf));
        Assert.Null(CallDetector.OtherCall(Set(), TitlesOf));
    }

    [Fact]
    public void KnownAppWinsOverBrowserMeeting()
    {
        var c = CallDetector.OtherCall(Set("chrome.exe", "zoom.exe"), TitlesOf);
        Assert.Equal("Zoom", c?.App);
        Assert.Equal("", c?.Title);
    }

    [Theory]
    [InlineData("Meet – abc-defg-hij - Google Chrome", "Meet – abc-defg-hij")]
    [InlineData("Webex - Microsoft​ Edge", "Webex")]
    [InlineData("Webex - Microsoft Edge", "Webex")]
    [InlineData("Jitsi Meet — Mozilla Firefox", "Jitsi Meet — Mozilla Firefox")]
    [InlineData("Jitsi Meet - Mozilla Firefox", "Jitsi Meet")]
    public void CleanWebTitle(string raw, string expected) =>
        Assert.Equal(expected, CallDetector.CleanWebTitle(raw));

    [Fact]
    public void GuessTitlesSkipsNavAndGeneric()
    {
        var titles = new[]
        {
            "Calendar | Microsoft Teams",
            "Chat | Jan Novák | Microsoft Teams",
            "Meeting | Microsoft Teams",
            "Kompaktní zobrazení schůzky | Microsoft Teams",
            "Team standup | Microsoft Teams",
            "Team standup | Microsoft Teams",
            "Some other app window",
            "Schůzka s: Jan Novák | Microsoft Teams",
        };
        Assert.Equal(new[] { "Team standup", "Schůzka s: Jan Novák" }, CallDetector.GuessTitles(titles));
        Assert.Equal("Team standup", CallDetector.GuessMeetingTitle(titles));
    }

    [Fact]
    public void GuessTitleCompactView()
    {
        // compact view title carries no subject of its own; the subject comes from the meeting window
        Assert.Null(CallDetector.GuessMeetingTitle(["Meeting compact view | Microsoft Teams", "Compact view | Microsoft Teams"]));
        Assert.Equal("Team week plan",
            CallDetector.GuessMeetingTitle(["Meeting compact view (1) | Microsoft Teams", "Team week plan | Microsoft Teams"]));
        Assert.Null(CallDetector.GuessMeetingTitle([]));
    }

    [Fact]
    public void GuessTitleScheduledWith()
    {
        Assert.Equal("Schůzka s: Jan Novák", CallDetector.GuessMeetingTitle(["Schůzka s: Jan Novák | Microsoft Teams"]));
    }

    [Theory]
    [InlineData(null, true)]
    [InlineData("  ", true)]
    [InlineData("Meeting", true)]
    [InlineData("Kalendář", true)]
    [InlineData("meeting compact view x", true)]
    [InlineData("Team standup", false)]
    public void IsGenericTitle(string? title, bool expected) =>
        Assert.Equal(expected, CallDetector.IsGenericTitle(title));

    [Fact]
    public void IsGenericTitle_does_not_depend_on_the_culture()
    {
        // the finalizer and the calendar used to have their own copies, one with a culture-sensitive StartsWith
        var before = System.Globalization.CultureInfo.CurrentCulture;
        try
        {
            System.Globalization.CultureInfo.CurrentCulture = new System.Globalization.CultureInfo("tr-TR");
            Assert.True(CallDetector.IsGenericTitle("MEETING COMPACT VIEW (2)"));
            Assert.True(CallDetector.IsGenericTitle("Kompaktní zobrazení schůzky"));
            Assert.False(CallDetector.IsGenericTitle("\u00admeeting compact view"));  // ordinal: an ignorable character counts
        }
        finally
        {
            System.Globalization.CultureInfo.CurrentCulture = before;
        }
    }

    [Fact]
    public void MeetingWindowAndPrejoinTitles()
    {
        Assert.True(CallDetector.IsPrejoinTitle("Připojení ke schůzce | X | Microsoft Teams"));
        Assert.False(CallDetector.IsPrejoinTitle("Připojení ke schůzce"));
        Assert.True(CallDetector.IsMeetingWindow("Team standup | Microsoft Teams"));
        Assert.False(CallDetector.IsMeetingWindow("Calendar | Microsoft Teams"));
        Assert.False(CallDetector.IsMeetingWindow("Join meeting | X | Microsoft Teams"));
        Assert.False(CallDetector.IsMeetingWindow("Team standup"));
    }

    [Fact]
    public void MicUsersFiltersOwnProcessAndReleasedKeys()
    {
        var entries = new[]
        {
            new MicConsentEntry("Zoom.exe-like", "Zoom.exe", 100, 0),
            new MicConsentEntry(@"C:#Python#python.exe", "python.exe", 100, 0),
            new MicConsentEntry(@"C:#App#recordingz-capture.exe", "recordingz-capture.exe", 100, 0),
            new MicConsentEntry(@"C:#Chrome#chrome.exe", "chrome.exe", 100, 200),  // released
            new MicConsentEntry("Never", "never.exe", 0, 0),                        // never used
        };
        Assert.Equal(new HashSet<string> { "zoom.exe" }, MicUsers.Current(entries));
    }

    [Fact]
    public void TeamsInUseNeedsAnActiveTeamsKey()
    {
        Assert.True(MicUsers.TeamsInUse([new MicConsentEntry("MSTeams_8wekyb3d8bbwe", "MSTeams_8wekyb3d8bbwe", 5, 0)]));
        Assert.False(MicUsers.TeamsInUse([new MicConsentEntry("MSTeams_8wekyb3d8bbwe", "MSTeams_8wekyb3d8bbwe", 5, 9)]));
        Assert.False(MicUsers.TeamsInUse([new MicConsentEntry("zoom.exe", "zoom.exe", 5, 0)]));
        Assert.False(MicUsers.TeamsInUse([]));
    }
}
