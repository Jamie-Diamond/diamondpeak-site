#!/usr/bin/env python3
"""FTP follows eFTP both ways, outside a small dead band (Jamie, 15 Oct 2026)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import thresholds as T

fails = 0
def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += not ok

class Fake:
    def __init__(self, ftp, eftp):
        self.ftp, self.eftp, self.put = ftp, eftp, None
    def get_sport_settings(self, sport):
        return {"ftp": self.ftp} if sport == "Ride" else {}
    def get_wellness(self, days=3):
        return [{"sportInfo": [{"type": "Ride", "eftp": self.eftp}] if self.eftp else []}]
    def _put(self, path, body):
        self.put = (path, body)

c = Fake(309, 297)
r = T.sync_ftp_from_eftp("x", {}, c, apply=True)
check("eFTP below FTP lowers it", r["applied"] and c.put == ("sport-settings/Ride", {"ftp": 297}))
c = Fake(250, 262)
r = T.sync_ftp_from_eftp("x", {}, c, apply=True)
check("eFTP above FTP raises it", r["applied"] and c.put[1] == {"ftp": 262})
c = Fake(270, 268)
r = T.sync_ftp_from_eftp("x", {}, c, apply=True)
check("a 2W wobble on 270 is left alone", not r["changed"] and c.put is None)
c = Fake(250, None)
r = T.sync_ftp_from_eftp("x", {}, c, apply=True)
check("no eFTP, no write", not r["changed"] and c.put is None)
c = Fake(309, 297)
r = T.sync_ftp_from_eftp("x", {}, c, apply=False)
check("dry run never writes", r["changed"] and c.put is None)

sys.exit(1 if fails else 0)
