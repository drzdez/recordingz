using System.Text;
using System.Text.RegularExpressions;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.Detection;

/// <summary>
/// The calls Teams itself reports as active, from its own log (new Teams:
/// %LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams\Logs\MSTeams_*.log). Only the call
/// tracker's lines are read ("TeamsCallTracker: Call became active: &lt;id&gt;", "… Call ended: &lt;id&gt;"); nothing
/// else of the log is kept, and nothing leaves this PC. The format is Teams' own and may change with an update, so
/// it never decides alone that a call is recorded: it warns when Teams is in a call that is not being recorded
/// (2026-11-11: the microphone looked unused after a Windows update, three meetings went unrecorded), keeps a
/// recording started from that warning running, and tells a call from the next one Teams joins straight after.
/// </summary>
public sealed class TeamsCallLog
{
    public static string DefaultDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Packages", "MSTeams_8wekyb3d8bbwe", "LocalCache", "Microsoft", "MSTeams", "Logs");

    /// <summary>A log file is read from its end minus this much when first seen (they rotate at about 2 MB).</summary>
    public const long FirstReadMax = 8 * 1024 * 1024;

    private static readonly Regex Active = new(@"TeamsCallTracker: Call became active: (\S+)", RegexOptions.CultureInvariant);
    private static readonly Regex Ended = new(@"TeamsCallTracker: Call ended: (\S+)", RegexOptions.CultureInvariant);

    private readonly string _dir;
    private readonly List<string> _active = [];
    private string? _file;
    private long _pos;
    private string _partial = "";
    private bool _failedLogged;

    public TeamsCallLog(string dir) => _dir = dir;

    /// <summary>Ids of the calls Teams reports active, oldest first.</summary>
    public IReadOnlyList<string> ActiveCalls => _active;

    /// <summary>The call Teams joined last and has not ended, or null.</summary>
    public string? Latest => _active.Count > 0 ? _active[^1] : null;

    /// <summary>Teams is not running: whatever its log said last, no call goes on.</summary>
    public void Reset() => _active.Clear();

    /// <summary>Reads what Teams wrote since the last poll (the rest of the current file, then a newer one). Never
    /// throws: a missing folder or a locked file just leaves the state as it was.</summary>
    public void Poll()
    {
        try
        {
            if (!Directory.Exists(_dir))
                return;
            if (_file is not null)
                ReadMore();
            // newest by name (MSTeams_<date>_<time>.<n>.log): Windows updates the size and time of a file that is
            // open for writing only lazily, a fresh file can look empty and older than the one before it
            var newest = Directory.GetFiles(_dir, "MSTeams_*.log").OrderBy(f => Path.GetFileName(f), StringComparer.Ordinal).LastOrDefault();
            if (newest is null || newest == _file)
                return;
            bool first = _file is null;
            _file = newest;
            _pos = -1;  // the end minus FirstReadMax on the first file, from its start on a rotated one
            _partial = "";
            ReadMore(first);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            if (!_failedLogged)
                Log.Warn($"Teams log not readable: {e.Message}");
            _failedLogged = true;
        }
    }

    private void ReadMore(bool first = false)
    {
        using var fs = new FileStream(_file!, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        if (_pos < 0)
            _pos = first ? Math.Max(0, fs.Length - FirstReadMax) : 0;
        if (fs.Length < _pos)  // the file was truncated: start over
            _pos = 0;
        if (fs.Length == _pos)
            return;
        fs.Seek(_pos, SeekOrigin.Begin);
        var buf = new byte[fs.Length - _pos];
        int n = 0, r;
        while (n < buf.Length && (r = fs.Read(buf, n, buf.Length - n)) > 0)
            n += r;
        _pos += n;
        Feed(Encoding.UTF8.GetString(buf, 0, n));
    }

    /// <summary>Takes text as it comes from the log (a line may be cut at the end; it is finished by the next
    /// call).</summary>
    internal void Feed(string text)
    {
        var all = _partial + text;
        var lines = all.Split('\n');
        _partial = lines[^1];
        for (int i = 0; i < lines.Length - 1; i++)
            FeedLine(lines[i]);
    }

    private void FeedLine(string line)
    {
        if (!line.Contains("TeamsCallTracker:", StringComparison.Ordinal))
            return;
        if (Active.Match(line) is { Success: true } a)
        {
            var id = a.Groups[1].Value;
            _active.Remove(id);
            _active.Add(id);
        }
        else if (Ended.Match(line) is { Success: true } e)
            _active.Remove(e.Groups[1].Value);
    }
}
