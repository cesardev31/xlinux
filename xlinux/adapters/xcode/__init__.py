"""Native Xcode adapter: SwiftUI / UIKit apps with an .xcodeproj."""

from ...core import device
from .build import is_xcode_project

NAME = "Xcode (SwiftUI / UIKit)"


def setup():
    pass  # uses the Swift toolchain and iOS SDK every adapter needs


def doctor_checks(_which):
    return []


def detects(project_dir):
    return is_xcode_project(project_dir)


def run(project):
    """Launch the installed app and stream its logs (print() output included)."""
    device.launch(project.bundle_identifier())
    device.stream_logs(project.executable)
