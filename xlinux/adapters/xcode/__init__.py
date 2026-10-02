"""Native Xcode adapter: SwiftUI / UIKit apps with an .xcodeproj."""

from ...core import device
from ...core.util import log
from .build import is_xcode_project

NAME = "Xcode (SwiftUI / UIKit)"


def setup():
    pass  # uses the Swift toolchain and iOS SDK every adapter needs


def doctor_checks(_which):
    return []


def detects(project_dir):
    return is_xcode_project(project_dir)


def run(project):
    """Launch the installed app and print what its own code logs (os_log /
    Logger / NSLog), leaving system frameworks' noise out like Xcode does."""
    device.launch(project.bundle_identifier())
    log("App logs (Ctrl+C to stop):")
    device.forward_logs(project.executable, "\0").wait()  # no prefix: only the app's own messages
