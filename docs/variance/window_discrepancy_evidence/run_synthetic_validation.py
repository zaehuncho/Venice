"""Full-count synthetic estimator exercise; not experimental or runtime evidence."""
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/"tools"/"timing"))
import sweep_analysis as s
from test_sweep_analysis import synthetic_rows

start=time.time()
r=s.trial_analysis(synthetic_rows(200),protocol_pass=True)
out=Path(__file__).with_name("synthetic_validation.json")
out.write_text(json.dumps(r,indent=2,allow_nan=False),encoding="utf-8")
lines=["SYNTHETIC ONLY "+r["status"],
       "fidelity "+str(r["fidelity"]["pass_all"]),
       "gof {successful} / {requested}".format(**r["primary"]["goodness_of_fit"]),
       "ff_bootstrap {bootstrap_successful} / {bootstrap_requested}".format(**r["FF"]["adjusted"]),
       "width "+r["width"]["status"],
       "seconds "+str(round(time.time()-start,2))]
text="\n".join(lines)+"\n"
out.with_suffix(".txt").write_text(text,encoding="utf-8")
print(text,end="")
