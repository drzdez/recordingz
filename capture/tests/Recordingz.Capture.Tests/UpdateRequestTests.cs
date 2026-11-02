using System.Text.Json;
using Recordingz.Capture.App;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.Tests;

public class UpdateRequestTests
{
    [Fact]
    public void The_review_page_asks_to_check_or_install_and_nothing_else()
    {
        var dir = Path.Combine(Path.GetTempPath(), $"recordingz-update-{Guid.NewGuid():N}");
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, UpdateRequest.FileName);
        try
        {
            File.WriteAllText(path, "install");  // left over: a start must never install by itself
            var asked = new System.Collections.Concurrent.ConcurrentQueue<string>();
            using var watcher = UpdateRequest.Watch(dir, asked.Enqueue);
            Assert.NotNull(watcher);
            Assert.False(File.Exists(path));

            File.WriteAllText(path, "format c:");
            Assert.True(SpinWait.SpinUntil(() => !File.Exists(path), 3000));
            File.WriteAllText(path, "check\n");
            Assert.True(SpinWait.SpinUntil(() => asked.Contains("check"), 5000));
            Assert.True(SpinWait.SpinUntil(() => !File.Exists(path), 2000));
            Assert.DoesNotContain(asked, a => a != "check");
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public void The_answer_says_the_state_and_both_versions()
    {
        var path = Path.Combine(Path.GetTempPath(), $"recordingz-update-{Guid.NewGuid():N}.json");
        try
        {
            UpdateRequest.WriteState("available", "9.9.9", "", path);
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var r = doc.RootElement;
            Assert.Equal("available", r.GetProperty("state").GetString());
            Assert.Equal(Versions.AppVersion, r.GetProperty("current").GetString());
            Assert.Equal("9.9.9", r.GetProperty("available").GetString());
            Assert.Equal(Environment.ProcessId, r.GetProperty("pid").GetInt32());
            UpdateRequest.WriteState("current", null, "", path);
            using var again = JsonDocument.Parse(File.ReadAllText(path));
            Assert.Equal(JsonValueKind.Null, again.RootElement.GetProperty("available").ValueKind);
        }
        finally
        {
            File.Delete(path);
        }
    }
}
