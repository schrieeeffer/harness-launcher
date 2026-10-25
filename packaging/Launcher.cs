using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Harness Launcher")]
[assembly: AssemblyVersion("0.4.2.0")]
[assembly: AssemblyFileVersion("0.4.2.0")]
internal static class Launcher {
    [STAThread]
    private static void Main() {
        try {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string python = Path.Combine(root, "runtime", "pythonw.exe");
            string script = Path.Combine(root, "launcher.py");
            if (!File.Exists(python) || !File.Exists(script))
                throw new IOException("请先把 ZIP 完整解压到一个文件夹，再双击 Harness Launcher.exe。不要只复制 exe，也不要直接在压缩包中运行。");
            var start = new ProcessStartInfo(python, "-B \"" + script + "\"");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            Process.Start(start);
        } catch (Exception error) {
            MessageBox.Show(error.Message, "Harness Launcher", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
