using Recordingz.Capture.App;

namespace Recordingz.Capture.Tests;

public class UpdaterTests
{
    // trimmed answer of GET /repos/drzdez/recordingz/releases/latest 
    private const string Latest = """
        {"tag_name": "v2.0.0", "html_url": "https://github.com/drzdez/recordingz/releases/tag/v2.0.0",
         "draft": false, "prerelease": false,
         "assets": [
           {"name": "notes.txt", "size": 10, "browser_download_url": "https://github.com/drzdez/recordingz/releases/download/v2.0.0/notes.txt"},
           {"name": "recordingz-2.0.0-x64.msi", "size": 46362624,
            "digest": "sha256:D2899080D4B925278F65EB9E4166A115EF72C407F050AF47B1DFC550BCFA9556",
            "browser_download_url": "https://github.com/drzdez/recordingz/releases/download/v2.0.0/recordingz-2.0.0-x64.msi"}
         ]}
        """;

    [Fact]
    public void The_latest_release_gives_its_msi_with_size_and_hash()
    {
        var rel = UpdateLogic.Parse(Latest);
        Assert.NotNull(rel);
        Assert.Equal("2.0.0", rel.Version);
        Assert.Equal("recordingz-2.0.0-x64.msi", rel.MsiName);
        Assert.Equal(46362624, rel.Size);
        Assert.Equal("d2899080d4b925278f65eb9e4166a115ef72c407f050af47b1dfc550bcfa9556", rel.Sha256);
        Assert.True(UpdateLogic.TrustedUrl(rel.MsiUrl));
    }

    [Fact]
    public void A_release_without_an_msi_or_a_prerelease_is_not_offered()
    {
        Assert.Null(UpdateLogic.Parse("""{"tag_name": "v2.0.0", "assets": []}"""));
        Assert.Null(UpdateLogic.Parse(Latest.Replace("\"prerelease\": false", "\"prerelease\": true")));
        Assert.Null(UpdateLogic.Parse(Latest.Replace("v2.0.0\", \"html_url", "nightly\", \"html_url")));
        Assert.False(UpdateLogic.TrustedUrl("https://example.com/recordingz-2.0.0-x64.msi"));
        Assert.False(UpdateLogic.TrustedUrl("https://github.com/someone/else/releases/download/v1/x-x64.msi"));
    }

    [Theory]
    [InlineData("1.0.8", "1.0.7", true)]
    [InlineData("1.1.0", "1.0.10", true)]
    [InlineData("1.0.10", "1.0.9", true)]  // numeric, not text
    [InlineData("1.0.7", "1.0.7", false)]
    [InlineData("1.0.6", "1.0.7", false)]
    [InlineData("garbage", "1.0.7", false)]
    public void Only_a_higher_version_is_newer(string candidate, string current, bool newer) =>
        Assert.Equal(newer, UpdateLogic.IsNewer(candidate, current));

    [Fact]
    public void A_check_is_due_at_start_then_once_a_day()
    {
        var now = new DateTime(2026, 10, 1, 9, 0, 0);
        Assert.True(UpdateLogic.Due(null, now));
        Assert.False(UpdateLogic.Due(now.AddHours(-23), now));
        Assert.True(UpdateLogic.Due(now.AddDays(-1), now));
        Assert.True(UpdateLogic.Due(now.AddDays(2), now));  // the clock went back: check rather than never
    }

    [Fact]
    public void Only_a_clear_no_declines_the_version()
    {
        Assert.True(UpdateLogic.Declines(Dialogs.Choice(7)));       // IDNO
        Assert.False(UpdateLogic.Declines(Dialogs.Choice(32000)));  // the box timed out: offered again
        Assert.False(UpdateLogic.Declines(Dialogs.Choice(0)));      // the box failed
        Assert.Equal(true, Dialogs.Choice(6));
    }

    [Fact]
    public void Old_installers_in_temp_are_cleaned_up_but_a_newer_one_is_kept()
    {
        Assert.True(UpdateLogic.StaleDownload("recordingz-2.0.0-x64.msi", "2.0.0"));
        Assert.True(UpdateLogic.StaleDownload("recordingz-1.9.3-x64.msi", "2.0.0"));
        Assert.True(UpdateLogic.StaleDownload("recordingz-2.1.0-x64.msi.part", "2.0.0"));
        Assert.False(UpdateLogic.StaleDownload("recordingz-2.1.0-x64.msi", "2.0.0"));
        Assert.False(UpdateLogic.StaleDownload("recordingz-nightly-x64.msi", "2.0.0"));
        Assert.False(UpdateLogic.StaleDownload("other-1.0.0-x64.msi", "2.0.0"));

        var dir = TempDir();
        try
        {
            foreach (var n in new[] { "recordingz-1.9.3-x64.msi", "recordingz-2.1.0-x64.msi", "recordingz-2.1.0-x64.msi.part", "other.msi" })
                File.WriteAllText(Path.Combine(dir, n), "");
            Updater.CleanDownloads(dir, "2.0.0");
            Assert.Equal(["other.msi", "recordingz-2.1.0-x64.msi"],
                         Directory.GetFiles(dir).Select(Path.GetFileName).Order(StringComparer.Ordinal));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    private static string TempDir()
    {
        var dir = Path.Combine(Path.GetTempPath(), "recordingz-upd-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return dir;
    }

    private static Release Rel(long size, string? sha = null) =>
        new("2.1.0", "recordingz-2.1.0-x64.msi",
            "https://github.com/drzdez/recordingz/releases/download/v2.1.0/recordingz-2.1.0-x64.msi", size, sha, "");

    /// <summary>GitHub as a fake: every request answers 200 with the given body.</summary>
    private sealed class FakeGitHub(Func<HttpContent> body) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct) =>
            Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.OK) { Content = body() });
    }

    /// <summary>A body that delivers its first bytes and then nothing more (a dead connection).</summary>
    private sealed class Stalled(byte[] first) : Stream
    {
        private bool _sent;
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() { }
        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

        public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken ct = default)
        {
            if (!_sent)
            {
                _sent = true;
                first.CopyTo(buffer);
                return first.Length;
            }
            await Task.Delay(Timeout.Infinite, ct);
            return 0;
        }

        public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken ct) =>
            ReadAsync(buffer.AsMemory(offset, count), ct).AsTask();
    }

    [Fact]
    public async Task A_stalled_download_is_given_up_and_leaves_no_file()
    {
        var dir = TempDir();
        try
        {
            using var http = new HttpClient(new FakeGitHub(() => new StreamContent(new Stalled(new byte[100]))));
            var e = await Assert.ThrowsAsync<InvalidOperationException>(
                () => Updater.DownloadAsync(http, Rel(1000), dir, TimeSpan.FromMilliseconds(300)));
            Assert.Contains("stalled", e.Message);
            Assert.Empty(Directory.GetFiles(dir));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public async Task A_short_or_wrong_download_leaves_no_file_and_a_good_one_gets_its_name()
    {
        var dir = TempDir();
        try
        {
            var body = new byte[1000];
            body[0] = 42;
            var sha = Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(body));
            using var http = new HttpClient(new FakeGitHub(() => new ByteArrayContent(body)));

            var e = await Assert.ThrowsAsync<InvalidOperationException>(
                () => Updater.DownloadAsync(http, Rel(2000), dir, TimeSpan.FromSeconds(10)));
            Assert.Contains("2000", e.Message);
            Assert.Empty(Directory.GetFiles(dir));

            await Assert.ThrowsAsync<InvalidOperationException>(
                () => Updater.DownloadAsync(http, Rel(1000, new string('0', 64)), dir, TimeSpan.FromSeconds(10)));
            Assert.Empty(Directory.GetFiles(dir));

            var path = await Updater.DownloadAsync(http, Rel(1000, sha), dir, TimeSpan.FromSeconds(10));
            Assert.Equal(Path.Combine(dir, "recordingz-2.1.0-x64.msi"), path);
            Assert.Equal([path], Directory.GetFiles(dir));
            Assert.Equal(body, File.ReadAllBytes(path));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }
}
