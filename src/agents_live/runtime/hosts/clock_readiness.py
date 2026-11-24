"""Exercise the packaged Windows clock renderer against Task Scheduler."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

from . import task_scheduler


_PROBE = r"""
param($Directory, $FolderName)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$root = $service.GetFolder('\')
$folder = $root.CreateFolder($FolderName)
$registered = @()
try {
    foreach ($index in 0,1) {
        $xml = [System.IO.File]::ReadAllText((Join-Path $Directory "$index.xml"))
        $task = $folder.RegisterTask("clock-$index", $xml, 6, $null, $null, 3, $null)
        $registered += "clock-$index"
        [System.IO.File]::WriteAllText(
            (Join-Path $Directory "registered-$index.xml"), $task.Xml)
    }
    $deadline = (Get-Date).AddSeconds(145)
    do {
        Start-Sleep -Seconds 1
        if ((Test-Path -LiteralPath (Join-Path $Directory '0.txt')) -and
            (Test-Path -LiteralPath (Join-Path $Directory '1.txt'))) {
            if (@(Get-Content -LiteralPath (Join-Path $Directory '0.txt')).Count -gt 0 -and
                @(Get-Content -LiteralPath (Join-Path $Directory '1.txt')).Count -gt 0) {
                Write-Output 'clock launched at both local anchors'
                exit 0
            }
        }
    } while ((Get-Date) -lt $deadline)
    throw "clock never launched at both anchors within 145 seconds; LastTaskResult=$($task.LastTaskResult)"
} finally {
    foreach ($name in $registered) { $folder.DeleteTask($name, 0) }
    if ($folder.GetTasks(0).Count -ne 0) { throw 'probe folder is not empty' }
    $root.DeleteFolder($FolderName, 0)
}
"""


def check() -> str:
    """Prove PT9M triggers launch at two future local anchors within three minutes."""
    if sys.platform != "win32":
        return "skipped: native clock readiness requires Windows"
    with tempfile.TemporaryDirectory(prefix="agents-live-clock-") as temporary:
        directory = Path(temporary)
        command = task_scheduler.hidden_host()
        if command is None:
            raise task_scheduler.TaskError("native clock readiness requires pythonw")
        folder = "AgentsLiveProbe-" + uuid4().hex
        now = datetime.now().astimezone()
        anchors = [
            (now + timedelta(minutes=index + 1)).replace(second=0, microsecond=0)
            for index in range(2)
        ]
        identities = []
        for index, anchor in enumerate(anchors):
            output = directory / f"{index}.txt"
            action = directory / f"stamp-{index}.py"
            action.write_text(
                "from datetime import datetime\n"
                "from pathlib import Path\n"
                f"with Path({str(output)!r}).open('a', encoding='utf-8') as stream:\n"
                "    stream.write(datetime.now().astimezone().isoformat() + '\\n')\n",
                encoding="utf-8",
            )
            document = task_scheduler.build_task_xml(
                command=str(command),
                arguments=task_scheduler.argument_string([str(action)]),
                working_dir=str(directory), schedules=["0,9 0 * * *"],
                description="Isolated native clock readiness",
                uri=f"\\{folder}\\clock-{index}",
                user_id=task_scheduler.current_user_id(),
            )
            task = ET.fromstring(document.split("?>", 1)[-1].strip())
            ns = {"t": task_scheduler._NS}
            boundary = task.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", ns)
            duration = task.find("t:Triggers/t:CalendarTrigger/t:Repetition/t:Duration", ns)
            if boundary is None or duration is None:
                raise task_scheduler.TaskError("native clock renderer omitted its repeating trigger")
            # Move the PT9M test's anchor ahead instead of waiting until midnight.
            boundary.text = task_scheduler._wall_clock(anchor)
            duration.text = "P1D"
            identities.append(task_scheduler.registered_trigger_identity(
                ET.tostring(task, encoding="unicode")))
            (directory / f"{index}.xml").write_bytes(ET.tostring(task, encoding="utf-16"))
        script = directory / "probe.ps1"
        script.write_text(_PROBE, encoding="utf-8")
        powershell = (
            Path(os.environ["SystemRoot"]) / "System32"
            / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-File",
             str(script), str(directory), folder],
            capture_output=True, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode:
            raise task_scheduler.TaskError(
                completed.stderr.strip() or completed.stdout.strip()
                or "native clock readiness failed")
        observed = []
        for index, anchor in enumerate(anchors):
            registered = (directory / f"registered-{index}.xml").read_text(encoding="utf-8-sig")
            if task_scheduler.registered_trigger_identity(registered) != identities[index]:
                raise task_scheduler.TaskError("native clock readback changed its UTC anchor or offset")
            fires = (directory / f"{index}.txt").read_text(encoding="utf-8").splitlines()
            fired = datetime.fromisoformat(fires[0])
            delay = (fired - anchor).total_seconds()
            if len(fires) != 1 or not 0 <= delay < 30:
                raise task_scheduler.TaskError(
                    f"native clock expected {anchor.isoformat()}, observed {fires}")
            observed.append(fired.isoformat())
        return "clock launched at expected local minutes: " + ", ".join(observed)


if __name__ == "__main__":
    print(check())
