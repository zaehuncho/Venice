"""Hash-checked documentation/draft-analysis transaction; never operates on Orion runtime."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shutil

HERE=Path(__file__).resolve().parent
LIVE=HERE.parents[2]
FILES=["docs/variance/PREREGISTRATION_OFFSET_SWEEP.md","docs/variance/WINDOW_DISCREPANCY.md",
       "tools/timing/sweep_analysis.py","tools/timing/test_sweep_analysis.py"]
BEFORE="B0B1034B64603A875A1107300CB3C3A34423F4EDA6DE8E49D161F53C40601339"


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest().upper()


def checked(root,relative):
    p=root/relative
    assert p.resolve().is_relative_to(root.resolve()),"path escaped target"
    assert not any(x.is_symlink() for x in [p]+list(p.parents) if x.is_relative_to(root)),"symbolic link target"
    if p.exists():assert p.stat().st_nlink==1,"hard-linked target"
    return p


def prepare(copies=True):
    assert sha(HERE/"PREREGISTRATION_OFFSET_SWEEP.before.md")==BEFORE
    manifest=[];diff=[]
    for rel in FILES:
        after=(LIVE/rel).read_text(encoding="utf-8")
        before=(HERE/"PREREGISTRATION_OFFSET_SWEEP.before.md").read_text(encoding="utf-8") if rel==FILES[0] else ""
        manifest.append(dict(path=rel,before_sha256=BEFORE if rel==FILES[0] else None,after_sha256=sha(LIVE/rel)))
        diff.append(f"diff --git a/{rel} b/{rel}\n")
        if not before:diff.append("new file mode 100644\n")
        diff.extend(difflib.unified_diff(before.splitlines(keepends=True),after.splitlines(keepends=True),
                                        fromfile="a/"+rel if before else "/dev/null",tofile="b/"+rel))
    (HERE/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    (HERE/"DIFF_FILE.patch").write_text("".join(diff),encoding="utf-8",newline="\n")
    if not copies:
        print("REFRESHED: manifest and replayable four-file patch; original backup unchanged")
        return
    for name,modified in (("baseline_copy",False),("rollback_copy",True),("replay_copy",False)):
        root=HERE/name;root.mkdir(exist_ok=False)
        for rel in FILES if modified else FILES[:1]:
            p=checked(root,rel);p.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(LIVE/rel if modified else HERE/"PREREGISTRATION_OFFSET_SWEEP.before.md",p)
    print("PREPARED: original preserved; four-file patch, baseline, replay and rollback copies created")


def verify(root,state):
    manifest=json.loads((HERE/"manifest.json").read_text())
    for entry in manifest:
        p=checked(root,entry["path"])
        expected=entry["before_sha256"] if state=="baseline" else entry["after_sha256"]
        assert (sha(p)==expected if expected else not p.exists()),f"unexpected {state} file: {p}"
    if state=="baseline":
        print("BASELINE: preregistration SHA256="+BEFORE+"; new report, estimator and tests absent")
    else:print("MODIFIED: 4/4 files match frozen manifest; estimator, tests and report present; preregistration amended")


def rollback(root):
    assert root.resolve()!=LIVE.resolve(),"rollback demonstration is copy-only; live deliverables are preserved"
    verify(root,"modified")  # Revalidate ALL targets before mutating any of them.
    backup=HERE/"PREREGISTRATION_OFFSET_SWEEP.before.md"
    assert sha(backup)==BEFORE
    for rel in FILES:
        p=checked(root,rel)
        if rel==FILES[0]:p.write_bytes(backup.read_bytes())
        else:p.unlink()  # Only these three individually hash-checked new files, never recursive deletion.
    verify(root,"baseline")
    print("ROLLBACK: original preregistration restored byte-for-byte; draft/report/tests absent in copy; live deliverables unchanged")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("prepare","refresh","baseline","modified","rollback"))
    parser.add_argument("--root",type=Path,default=LIVE)
    args=parser.parse_args()
    if args.action=="prepare":prepare()
    elif args.action=="refresh":prepare(copies=False)
    elif args.action=="rollback":rollback(args.root.resolve())
    else:verify(args.root.resolve(),args.action)
