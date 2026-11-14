"""旧信念出处回填：默认只演练，--apply 才写。

只把 persona 旧档案里同一文本的原始行登记为 legacy_note 事件，
belief_evidence.relation='origin'。不升级 legacy、不写置信度、不造经历。
"""
import argparse
import json
import sys

import self_model


def main(argv=None):
    parser = argparse.ArgumentParser(description="legacy 信念出处回填")
    parser.add_argument("--apply", action="store_true",
                        help="真正写入 origin 链接；默认只演练")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = self_model.backfill_legacy_provenance(apply=args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        print("mode=%s scanned=%s linked=%s unmatched=%s multi=%s failed=%s"
              % ("apply" if args.apply else "dry-run", result.get("scanned"),
                 result.get("linked"), len(result.get("unmatched") or []),
                 len(result.get("multi") or []), len(result.get("failed") or [])))
        for belief_id in (result.get("unmatched") or [])[:10]:
            print("unmatched", belief_id)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())