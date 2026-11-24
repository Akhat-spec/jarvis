// Однофайловый установщик J.A.R.V.I.S.: распаковывает вложенный payload.zip во временную папку
// и запускает bootstrap.ps1 (поиск Python + мастер установки). Собирается installer\build.py.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("J.A.R.V.I.S. Setup")]
[assembly: AssemblyProduct("J.A.R.V.I.S.")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]

static class Setup
{
    [STAThread]
    static int Main()
    {
        string tmp = Path.Combine(Path.GetTempPath(), "JARVIS-Setup-" + Guid.NewGuid().ToString("N").Substring(0, 8));
        try
        {
            Directory.CreateDirectory(tmp);
            string zip = Path.Combine(tmp, "payload.zip");
            using (Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
            using (FileStream f = File.Create(zip))
                s.CopyTo(f);
            ZipFile.ExtractToDirectory(zip, tmp);
            File.Delete(zip);

            var psi = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + Path.Combine(tmp, "bootstrap.ps1") + "\"");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WorkingDirectory = tmp;
            using (Process p = Process.Start(psi))
            {
                Log("started " + p.Id);
                ShowSplashUntilReady(p);
                Log("splash closed");
                p.WaitForExit();
                Log("boot exited " + p.ExitCode);
                return p.ExitCode;
            }
        }
        catch (Exception e)
        {
            MessageBox.Show("Не удалось запустить установку:\n" + e.Message, "Установка J.A.R.V.I.S.",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            try { Directory.Delete(tmp, true); } catch (Exception e) { Log("delete: " + e.Message); }
            Log("done");
        }
    }

    static void Log(string s)
    {
        string f = Environment.GetEnvironmentVariable("JARVIS_SETUP_LOG");
        if (!string.IsNullOrEmpty(f)) File.AppendAllText(f, DateTime.Now.ToString("HH:mm:ss.fff ") + s + "\r\n");
    }

    // Заставка «Подготовка установки…», пока не появится окно мастера (или вопрос про Python)
    static void ShowSplashUntilReady(Process boot)
    {
        Application.EnableVisualStyles();
        var form = new Form();
        form.FormBorderStyle = FormBorderStyle.None;
        form.StartPosition = FormStartPosition.CenterScreen;
        form.Size = new Size(420, 120);
        form.BackColor = Color.FromArgb(10, 16, 23);
        form.Text = "J.A.R.V.I.S.";
        form.TopMost = true;
        try { form.Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch { }
        var title = new Label();
        title.Text = "●  J.A.R.V.I.S.";
        title.ForeColor = Color.FromArgb(46, 230, 200);
        title.Font = new Font("Segoe UI", 13, FontStyle.Bold);
        title.SetBounds(24, 22, 380, 30);
        var sub = new Label();
        sub.Text = "Подготовка установки…";
        sub.ForeColor = Color.FromArgb(127, 144, 157);
        sub.Font = new Font("Segoe UI", 10);
        sub.SetBounds(26, 60, 380, 24);
        form.Controls.Add(title);
        form.Controls.Add(sub);
        var timer = new Timer();
        timer.Interval = 300;
        timer.Tick += (s, e) =>
        {
            bool ready = boot.HasExited;
            if (!ready)
                foreach (Process pr in Process.GetProcesses())
                {
                    try { if (pr.MainWindowTitle.StartsWith("Установка J.A.R.V.I.S.")) { ready = true; break; } }
                    catch { }
                    finally { pr.Dispose(); }
                }
            if (ready) { timer.Stop(); form.Close(); }
        };
        timer.Start();
        Application.Run(form);
    }
}
