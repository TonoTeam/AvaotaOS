"""Onboard action: change the input method without moving browser focus."""
import subprocess


def run():
    subprocess.Popen(['fcitx5-remote', '-t'])
