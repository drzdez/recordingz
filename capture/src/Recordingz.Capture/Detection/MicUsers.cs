using Microsoft.Win32;
using Recordingz.Capture.Audio;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.Detection;

/// <summary>
/// One entry of Windows' microphone privacy registry. KeyName is the raw subkey name (a package family name,
/// or for NonPackaged apps the exe path with '\' replaced by '#'); Ident is the package name or the exe file name.
/// Start/Stop are LastUsedTimeStart/LastUsedTimeStop (FILETIME); Stop == 0 means "holding the mic right now".
/// </summary>
public sealed record MicConsentEntry(string KeyName, string Ident, long Start, long Stop);

/// <summary>The microphone right now: does Teams hold it, and every app that does (lower case idents).</summary>
public sealed record MicState(bool Teams, HashSet<string> Users);

/// <summary>
/// Who holds the microphone right now, from HKCU\...\CapabilityAccessManager\ConsentStore\microphone.
/// Windows writes LastUsedTimeStop = 0 while an app captures, which needs no audio API and no admin rights.
/// Since 2.0.12 also from the Core Audio capture sessions (<see cref="Now"/>), as the registry can stay silent.
/// </summary>
public static class MicUsers
{
    public const string ConsentKey =
        @"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone";

    /// <summary>
    /// This recorder itself holds the microphone while it records; it must not look like a call.
    /// python(w).exe = the prototype (may run side by side), recordingz-capture.exe = this app.
    /// </summary>
    public static readonly IReadOnlySet<string> NotACall = new HashSet<string>(StringComparer.Ordinal)
    {
        "python.exe", "pythonw.exe", "recordingz-capture.exe",
    };

    /// <summary>True while Teams holds the microphone: the privacy registry (LastUsedTimeStop == 0) or an active
    /// Core Audio capture session of Teams.</summary>
    public static bool TeamsInUse() => Now().Teams;

    /// <summary>
    /// Who holds the microphone right now, from both sources: the privacy registry and the Core Audio capture
    /// sessions. The registry alone missed every call after a Windows update (2026-11-11: KB5132061/KB5132688,
    /// three Teams meetings, not one registry entry since the restart); the session list asks the audio engine
    /// itself, whichever app records.
    /// </summary>
    public static MicState Now()
    {
        var entries = ReadEntries();
        var sessions = Devices.CaptureSessions();
        var tree = ProcessTree.Snapshot();
        var me = new HashSet<int> { Environment.ProcessId };
        if (tree.TryGetValue(Environment.ProcessId, out var self))
            me.Add(self.Parent);
        var bySessions = SessionIdents(sessions, pid => HostName(tree, pid), pid => ProcessTree.IsTeams(tree, pid), me);
        var state = Combine(entries, bySessions);
        NoteSource(TeamsInUse(entries), bySessions.Contains(TeamsIdent));
        return state;
    }

    /// <summary>The ident a Teams capture session gets (its audio may run in a helper process).</summary>
    public const string TeamsIdent = "ms-teams.exe";

    /// <summary>Exe names (lower case) of the processes with an active capture session; Teams and its helper
    /// processes as <see cref="TeamsIdent"/>; this recorder (and the tray that started it) left out.</summary>
    public static HashSet<string> SessionIdents(IEnumerable<CaptureSession> sessions, Func<int, string> processName,
                                                Func<int, bool> isTeams, ISet<int> me)
    {
        var own = OwnExeName();
        var output = new HashSet<string>(StringComparer.Ordinal);
        foreach (var s in sessions)
        {
            if (s.ProcessId == 0 || me.Contains(s.ProcessId))
                continue;
            var ident = isTeams(s.ProcessId) ? TeamsIdent : (processName(s.ProcessId) ?? "").ToLowerInvariant();
            if (ident.Length > 0 && !NotACall.Contains(ident) && ident != own && !ident.StartsWith("recordingz"))
                output.Add(ident);
        }
        return output;
    }

    /// <summary>The exe name of a process; for a WebView2 process (WhatsApp, the new Outlook...) the app that hosts
    /// it, so the call app is named, not its browser engine.</summary>
    internal static string HostName(IReadOnlyDictionary<int, (int Parent, string Name)> tree, int pid)
    {
        var name = "";
        for (int i = 0; i < 5 && tree.TryGetValue(pid, out var p); i++)
        {
            name = p.Name;
            if (name != "msedgewebview2.exe" || p.Parent == 0 || p.Parent == pid)
                break;
            pid = p.Parent;
        }
        return name;
    }

    /// <summary>Pure rule behind <see cref="Now()"/>: the registry's holders plus the session holders.</summary>
    public static MicState Combine(IEnumerable<MicConsentEntry> entries, ISet<string> sessionIdents)
    {
        var list = entries as IReadOnlyCollection<MicConsentEntry> ?? entries.ToList();
        var users = Current(list);
        users.UnionWith(sessionIdents);
        return new MicState(TeamsInUse(list) || sessionIdents.Contains(TeamsIdent), users);
    }

    private static bool? _registryMissed;

    // Says once in the log when only the session list sees Teams on the microphone, and when the registry is back.
    private static void NoteSource(bool registry, bool sessions)
    {
        if (registry == sessions)
        {
            if (registry && _registryMissed == true)
            {
                Log.Info("microphone: the Windows privacy registry sees Teams again");
                _registryMissed = false;
            }
            return;
        }
        if (sessions && _registryMissed != true)
        {
            Log.Warn("microphone: Teams records (Core Audio session), but the Windows privacy registry has no entry for it");
            _registryMissed = true;
        }
    }

    /// <summary>Pure rule behind <see cref="TeamsInUse()"/>.</summary>
    public static bool TeamsInUse(IEnumerable<MicConsentEntry> entries) =>
        entries.Any(e => e.Start != 0 && e.Stop == 0 && IsTeams(e.Ident));

    /// <summary>
    /// A Teams entry by its app identity (package name or exe file name), never by the full key path: this
    /// recorder was teamsrec-capture.exe until 2.0 (the suite was called teamsrec), a name that contains "teams", and
    /// while it recorded the microphone its own entry counted as a Teams call that never ends (the Python prototype ran as pythonw.exe and never hit this).
    /// </summary>
    internal static bool IsTeams(string ident)
    {
        var i = ident.ToLowerInvariant();
        return i.Contains("teams") && !i.StartsWith("recordingz") && !i.StartsWith("teamsrec") && i != OwnExeName() && !NotACall.Contains(i);
    }

    /// <summary>
    /// Apps holding the microphone right now (the registry and the capture sessions): packaged app names and exe file names, lower case.
    /// This recorder itself is left out.
    /// </summary>
    public static HashSet<string> Current() => Now().Users;

    /// <summary>Pure rule behind <see cref="Current()"/>.</summary>
    public static HashSet<string> Current(IEnumerable<MicConsentEntry> entries)
    {
        var own = OwnExeName();
        var output = new HashSet<string>(StringComparer.Ordinal);
        foreach (var e in entries)
        {
            var ident = e.Ident.ToLowerInvariant();
            if (e.Start != 0 && e.Stop == 0 && !NotACall.Contains(ident) && ident != own)
                output.Add(ident);
        }
        return output;
    }

    /// <summary>
    /// Reads every packaged subkey and every NonPackaged subkey. Keys without both timestamps are skipped
    /// (as the prototype's OSError path); a failing registry read is logged and yields nothing.
    /// </summary>
    public static List<MicConsentEntry> ReadEntries()
    {
        var output = new List<MicConsentEntry>();
        try
        {
            using var root = Registry.CurrentUser.OpenSubKey(ConsentKey);
            if (root is null)
                return output;
            foreach (var name in root.GetSubKeyNames())
            {
                if (name == "NonPackaged")
                    continue;
                AddEntry(root, name, name, output);
            }
            using var np = root.OpenSubKey("NonPackaged");
            if (np is not null)
                foreach (var name in np.GetSubKeyNames())
                    AddEntry(np, name, name.Split('#')[^1], output);
        }
        catch (Exception ex) when (ex is System.Security.SecurityException or IOException or UnauthorizedAccessException)
        {
            Log.Warn($"registry read failed: {ex.Message}");
        }
        return output;
    }

    private static void AddEntry(RegistryKey parent, string keyName, string ident, List<MicConsentEntry> output)
    {
        try
        {
            using var k = parent.OpenSubKey(keyName);
            if (k is null)
                return;
            var start = k.GetValue("LastUsedTimeStart");
            var stop = k.GetValue("LastUsedTimeStop");
            if (start is null || stop is null)
                return;
            output.Add(new MicConsentEntry(keyName, ident, ToLong(start), ToLong(stop)));
        }
        catch (Exception ex) when (ex is System.Security.SecurityException or IOException or UnauthorizedAccessException
                                       or InvalidCastException or FormatException or OverflowException)
        {
            // one unreadable key must not hide the others
        }
    }

    // REG_QWORD comes back as long; tolerate REG_DWORD (int) as well.
    private static long ToLong(object v) => v switch
    {
        long l => l,
        int i => i,
        _ => Convert.ToInt64(v, System.Globalization.CultureInfo.InvariantCulture),
    };

    private static string? _ownExe;

    // The exe name we actually run as (a renamed build must still not detect itself as a call).
    private static string OwnExeName()
    {
        if (_ownExe is not null)
            return _ownExe;
        try
        {
            var path = Environment.ProcessPath;
            _ownExe = string.IsNullOrEmpty(path) ? "" : Path.GetFileName(path).ToLowerInvariant();
        }
        catch
        {
            _ownExe = "";
        }
        return _ownExe;
    }
}
