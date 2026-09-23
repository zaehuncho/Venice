"""Offline regression tests. Temporary fixtures stay under docs/variance/.

Run: python -B tools/timing/test_sweep_analysis.py
No Orion imports, launches, builds, capture, or live network operations.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy import stats
import sweep_analysis as sa

ROOT=Path(__file__).resolve().parents[2]
TMP=ROOT/"docs"/"variance"/"window_discrepancy_evidence"


def stamp(t):
    return datetime.fromtimestamp(t/1000,timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")


def record(ep=9007199254740993,seq=700,start=1789992000000.,release=None,session="fixture"):
    release=start+600 if release is None else release
    return dict(session=session,epoch=ep,seq=seq,press_ts_ms=start,closed_ts_ms=release+700,
                release_ms=release,release_after_press_ms=release-start,onset_ms=200,
                onset_source="detect_loop",shot_type="Standstill",outcome="released",
                banner=dict(timing="EXCELLENT",coverage="WIDE OPEN"))


def log_lines(r,native_seq=4,attempt=17,Z=12.,D=None,A=1,F=-10.,scheduled=1,landing=True,draw=True):
    p=r["press_ts_ms"];t=r["release_ms"];ep=r["epoch"];D=Z if D is None else D
    lines=[(p,f"Physical shot epoch: epoch={ep}"),
           (p+20,f"TIP RESERVATION: physical_epoch={ep} shot_attempt={attempt}"),
           (p+30,f"ONSET FF: reason=applied onset_ms=200 ref_ms=180 dev_ms=20 applied_ms={F} n=5 gain={.45 if A==1 else .2} clamp_ms={40 if A==1 else 10} one_sided={0 if A==1 else 1} physical_epoch={ep} shot_attempt={attempt}"+(f" ab_arm={A}" if A is not None else "")),
           (t,f"Release issued: (green 93.0-96.7) seq={native_seq}"),
           (t,f"Release delivery identity: physical_epoch={ep} shot_attempt={attempt} release_seq={native_seq}"),
           (t,f"Release onsetff: seq={native_seq} physical_epoch={ep} shot_attempt={attempt} applied_ms={F} scheduled={scheduled}"),
           (t,f"Release devoffset: seq={native_seq} shot_attempt={attempt} applied_ms={D} scheduled={scheduled}"),
           (t,f"Scheduled fire: seq={native_seq} shot_attempt={attempt} scheduled={scheduled} deltaMs=0.2"),
           (t,f"Release attribution: seq={native_seq} greenConfirmed=1 greenWidth=3.7 greenConfirmMs=40 vel=0.18"),
           (t,f"Release timing: seq={native_seq} appearToRelMs=100 holdToRelMs=90")]
    if draw:lines.append((p+25,f"DEV FIRE OFFSET DRAW: shot_attempt={attempt} offset_ms={Z}"))
    if landing:lines.append((t+1200,f"Release landing: seq={native_seq} graded=1 green_obs_n=4 green_start=93.0 green_end=96.7 vel_at_rel=0.18"))
    return [stamp(t)+" "+s for t,s in sorted(lines,key=lambda v:v[0])]


def synthetic_rows(n=200,delivery=1.,seed=910):
    rng=np.random.default_rng(seed);Z=rng.uniform(-25,25,n);A=rng.integers(0,2,n);o=rng.normal(0,30,n)
    probabilities=sa.ordinal_probs(Z,A,-7.4,94.39,43.54,delta=-3.)
    y=(rng.random(n)[:,None]>np.cumsum(probabilities,axis=1)[:,:2]).sum(axis=1)
    rows=[]
    for i in range(n):
        r=dict(session="synthetic",epoch=i+1,press_ms=float(i*10000),release_ms=float(i*10000+600),hold_ms=600.,
               closed_ms=float(i*10000+1300),onset_ms=200+o[i],onset_dev=float(o[i]),reference_unavailable=0,
               shot_type="Standstill",outcome="released",grade=list(sa.GRADES)[y[i]],coverage_state="open",
               release_seq=i+201,join_source="release_ff",join_conflicts=[],primary_join=True,A=int(A[i]),
               Z_first=float(Z[i]),Z_final=float(Z[i]),D=float(Z[i]),F=float(-3*A[i]),scheduled=1,
               draw_count=1,arm_conflict=False,H=float(400+delivery*Z[i]+.2*o[i]-3*A[i]+rng.normal(0,1)),
               ff_n=5,scheduler_delta_ms=.2,pre_confirmed=True,pre_width_pp=float(rng.uniform(.6,4.9)))
        rows.append(r)
    return rows


class SweepTests(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix="unit_",dir=TMP)
        self.path=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def join(self,records,lines):
        p=self.path/"native.log";p.write_text("\n".join(lines),encoding="utf-8")
        ev,counts=sa.load_events([p]);return sa.join_shots(records,ev),ev,counts

    def test_curve_not_release_velocity(self):
        self.assertAlmostEqual(sa.curve_window(90,100),41.1)
        self.assertAlmostEqual(sa.curve_window(93,96.7),15.207)
        self.assertIsNone(sa.curve_window(98,95))
        self.assertAlmostEqual(sa.curve_time(10),-10/.158)

    def test_exact_physical_key_native_seq_and_late_landing(self):
        r=record();rows,_,_=self.join([r],log_lines(r));x=rows[0]
        self.assertEqual(x["epoch"],9007199254740993)
        self.assertEqual(x["record_seq"],700);self.assertEqual(x["release_seq"],4)
        self.assertTrue(x["primary_join"])
        self.assertAlmostEqual(x["landing_width_ms"],15.207)
        self.assertGreater(r["release_ms"]+1200,r["closed_ts_ms"])
        self.assertAlmostEqual(x["H"],400)  # NOT native holdToRelMs=90.
        self.assertEqual((x["Z_first"],x["D"],x["F"],x["A"]),(12.,12.,-10.,1))

    def test_resets_and_forwarded_duplicate(self):
        a=record(ep=21,session="a");b=record(ep=1,session="b",start=a["press_ts_ms"]+10000)
        la=log_lines(a,native_seq=7);lb=log_lines(b,native_seq=1)
        rows,_,counts=self.join([a,b],la+lb+[la[0]])
        self.assertEqual(counts["process_partitions"],2)
        self.assertNotEqual(rows[0]["process"],rows[1]["process"])
        self.assertTrue(all(r["primary_join"] for r in rows))
        self.assertEqual(counts["identical_forwarded_lines"],1)

    def test_ambiguous_epoch_is_quarantined(self):
        a=record();b=dict(a,session="overlapping")
        rows,_,_=self.join([a,b],log_lines(a))
        self.assertTrue(all(r["release_seq"] is None and not r["primary_join"] for r in rows))

    def test_missing_assignments_are_not_zero(self):
        r=record();rows,_,_=self.join([r],log_lines(r,A=None,F=0,draw=False))
        self.assertIsNone(rows[0]["A"]);self.assertIsNone(rows[0]["Z_first"])
        self.assertFalse(rows[0]["primary_join"])

    def test_impossible_release_clock_blocks_inference(self):
        r=record();r['release_after_press_ms']+=10
        rows,_,_=self.join([r],log_lines(r))
        self.assertTrue(rows[0]['clock_failure'])
        self.assertFalse(sa.fidelity(rows,reps=0)['conditions']['record_release_clocks_valid'])

    def test_release_value_not_last_refinement(self):
        r=record();lines=log_lines(r)
        lines.append(stamp(r["release_ms"]+1)+" DEV FIRE OFFSET APPLIED: shot_attempt=17 applied_ms=-21")
        x=self.join([r],lines)[0][0]
        self.assertEqual(x["D"],12);self.assertEqual(len(x["dev_refinement_audit"]),1)

    def test_distinct_arm_token_draws_preserve_first_and_final(self):
        r=record();lines=log_lines(r,attempt=18,Z=20)
        lines.extend([stamp(r["press_ts_ms"]+5)+f" TIP RESERVATION: physical_epoch={r['epoch']} shot_attempt=17",
                      stamp(r["press_ts_ms"]+10)+" DEV FIRE OFFSET DRAW: shot_attempt=17 offset_ms=-15"])
        x=self.join([r],lines)[0][0]
        self.assertEqual((x["Z_first"],x["Z_final"],x["D"],x["draw_count"]),(-15,20,20,2))

    def test_in_tick_must_be_zero_and_arm_conflicts_fail(self):
        r=record();x=self.join([r],log_lines(r,scheduled=0,D=12))[0][0]
        self.assertFalse(x["primary_join"])
        lines=log_lines(r);lines.append(stamp(r["press_ts_ms"]+40)+f" ONSET FF: reason=applied physical_epoch={r['epoch']} shot_attempt=17 ab_arm=0")
        x=self.join([r],lines)[0][0]
        self.assertTrue(x["arm_conflict"]);self.assertIsNone(x["A"])

    def test_record_dedup_and_invalid_identity(self):
        r=record();p=self.path/"records.jsonl"
        p.write_text("\n".join(json.dumps(v) for v in [r,r,dict(r,banner={}),dict(r,epoch=1.25),"bad"]),encoding="utf-8")
        rr,c=sa.load_records([p]);self.assertEqual(rr,[])
        self.assertEqual(c["identical_duplicates"],1);self.assertEqual(c["conflicting_duplicates"],1)
        self.assertEqual(c["invalid_identity_or_interval"],1);self.assertEqual(c["invalid_json"],1)

    def test_newcombe_and_exact_permutation(self):
        x=sa.risk_difference(7,10,3,10)
        self.assertAlmostEqual(x["difference"],.4)
        self.assertLess(x["lower95"],0);self.assertGreater(x["upper95"],.4)
        # Symmetric equal-sized design: two-sided hypergeometric tail = Fisher exact.
        self.assertAlmostEqual(x["permutation_p_two_sided"],stats.fisher_exact([[7,3],[3,7]]).pvalue)
        self.assertEqual(sa.risk_difference(0,0,3,10)["status"],"missing_arm")

    def test_scale_nonidentification_demonstrated(self):
        D=np.linspace(-40,40,40);A=np.zeros(40);a=15.207/94.39
        p=sa.ordinal_probs(D,A,-7.415,94.39,43.54)
        q=sa.ordinal_probs(D,A,-7.415*a,94.39*a,43.54*a,slope=a)
        np.testing.assert_allclose(p,q,atol=1e-14)

    def test_broad_and_narrow_MLE_recovery(self):
        for W,sigma in ((94.39,43.54),(16.,7.38)):
            rng=np.random.default_rng(148);n=5000;D=rng.uniform(-25,25,n);A=rng.integers(0,2,n)
            p=sa.ordinal_probs(D,A,-7.4,W,sigma,delta=-3)
            y=(rng.random(n)[:,None]>np.cumsum(p,axis=1)[:,:2]).sum(axis=1)
            f=sa.fit_ordinal(D,A,y)
            self.assertTrue(f["success"],f)
            self.assertLess(abs(f["params"]["W"]/W-1),.2)
            self.assertLess(abs(f["params"]["sigma"]/sigma-1),.2)
            self.assertLess(abs(f["params"]["c"]+7.4),3)

    def test_fidelity_one_vs_029(self):
        good=sa.fidelity(synthetic_rows(),reps=20)
        bad=sa.fidelity(synthetic_rows(delivery=.29),reps=20)
        self.assertTrue(good["pass_all"],good["failed"])
        self.assertFalse(bad["pass_all"])
        self.assertLess(bad["first_stage_Z"]["upper90"],.8)

    def test_fidelity_uses_ungraded_releases_and_flags_multidraw(self):
        rows=synthetic_rows();rows[0]["grade"]=None
        out=sa.fidelity(rows,reps=0)
        self.assertEqual(out["funnel"]["local_clock_rows"],len(rows))
        for r in rows[:5]:r["draw_count"]=2
        self.assertFalse(sa.fidelity(rows,reps=0)["conditions"]["multiple_dev_draw_epochs_le2pct"])

    def test_ff_missing_banner_is_policy_failure_not_early(self):
        rows=synthetic_rows(100)
        r=next(r for r in rows if r["A"]==1);r["grade"]=None
        out=sa.ff_contrast(rows,reps=10)
        self.assertEqual(sum(out["policy_success"]["counts"][1::2]),100)
        self.assertEqual(sum(out["graded_only"]["counts"][1::2]),99)
        self.assertEqual(out["early_all_assigned"]["counts"][0],sum(r["A"]==1 and r["grade"]=="EARLY" for r in rows))

    def test_area_and_competing_curves(self):
        h=sa.hypothesis_predictions()["hypotheses"]
        self.assertGreater(h["broad94"]["uniform_sweep_EXCELLENT"],.65)
        self.assertLess(h["visible16"]["uniform_sweep_EXCELLENT"],.32)
        for v in h.values():self.assertLessEqual(v["uniform_sweep_EXCELLENT"],v["area_bound"]+1e-8)

    def test_no_primary_without_protocol_and_width_no_audit(self):
        rows=synthetic_rows()
        out=sa.trial_analysis(rows,protocol_pass=False,reps_gof=2,reps_ff=2,reps_fidelity=2)
        self.assertEqual(out["status"],"PRIMARY_SUPPRESSED")
        with patch.object(sa,"profile_intervals",return_value={}):
            w=sa.width_interaction(rows)
        self.assertFalse(w["confirmatory_eligible"])
        self.assertEqual(w["status"],"exploratory_only")

    def test_primary_estimator_smoke(self):
        rows=synthetic_rows()
        # Fit likelihoods and resample for real; profile API covered below.
        with patch.object(sa,"profile_intervals",return_value={}):
            out=sa.trial_analysis(rows,protocol_pass=True,reps_gof=3,reps_ff=3,reps_fidelity=3)
        self.assertEqual(out["status"],"ANALYSED")
        self.assertTrue(out["primary"]["success"])
        self.assertEqual(out["primary"]["goodness_of_fit"]["requested"],3)
        self.assertEqual(out["width"]["status"],"exploratory_only")
        # Exercise the real artifact serialization, not only Python return values.
        self.assertIn('ANALYSED',json.dumps(out,allow_nan=False))

    def test_profiles_contain_fit(self):
        rows=synthetic_rows(200);D=[r["D"] for r in rows];A=[r["A"] for r in rows];y=[sa.GRADES[r["grade"]] for r in rows]
        f=sa.fit_ordinal(D,A,y);p=sa.profile_intervals(D,A,y,f,parameters=("c","pmax"))
        for x in p.values():
            self.assertIsNotNone(x["lower95"]);self.assertIsNotNone(x["upper95"])
            self.assertLess(x["lower95"],x["estimate"]);self.assertGreater(x["upper95"],x["estimate"])

    def test_cohort_uses_first_presses_not_graded_survivors(self):
        a=record(ep=1);b=record(ep=2,start=a["press_ts_ms"]+10000)
        rows,ev,_=self.join([b],log_lines(a)+log_lines(b))
        chosen,cohort=sa.cohort_from_presses(rows,ev,a["press_ts_ms"],{"fixture"},1)
        self.assertEqual(chosen,[]);self.assertFalse(cohort["complete"])
        self.assertEqual(cohort["missing_or_ambiguous"][0]["epoch"],1)

    def test_output_refuses_overwrite(self):
        with self.assertRaises(FileExistsError):sa.write_outputs(self.path,[],{})


if __name__=="__main__":unittest.main(verbosity=2)
