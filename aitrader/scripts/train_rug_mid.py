"""Обучить модель рагов рынка от $200k по датасету learn: python scripts/train_rug_mid.py samples-*.jsonl
Цель: lo ≤ −50% за горизонт образца. Порог = 70-й перцентиль вероятности на молодых (<24ч) токенах
обучения (отсекаем 30% самых рискованных — проверено 17.10: train 15.10 / test 16.10)."""
import json
import pathlib
import pickle
import sys

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import rugmodel  # noqa: E402

S = [json.loads(l) for f in sys.argv[1:] for l in open(f)]
S = [s for s in S if s.get("src") == "mid" and not s.get("suspect") and (s.get("gap") or 0) <= 60 and s["hi"] < 20]
X = np.array([rugmodel.features(s.get("dex"), s.get("flow")) for s in S])
y = np.array([1 if s["lo"] <= -0.5 else 0 for s in S])
keep = [j for j in range(X.shape[1]) if len(np.unique(X[~np.isnan(X[:, j]), j])) >= 2]
def mk():
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, min_samples_leaf=50, random_state=0)
# порог — по прогнозам на НЕвиденных днях (на своих данных модель слишком уверена: порог выходил 0.0002)
day = np.array([int(s["t0"] // 86400) for s in S])
oof = np.full(len(S), np.nan)
for d in np.unique(day):
    tr, te = day != d, day == d
    if tr.sum() > 1000 and y[tr].sum() > 20:
        oof[te] = mk().fit(X[tr][:, keep], y[tr]).predict_proba(X[te][:, keep])[:, 1]
young = np.array([((s.get("dex") or {}).get("age_h") or 99) < 24 for s in S])
ok = young & ~np.isnan(oof)
thr = float(np.quantile(oof[ok], 0.70))
from sklearn.metrics import roc_auc_score
print(f"проверка по дням: AUC {roc_auc_score(y[~np.isnan(oof)], oof[~np.isnan(oof)]):.3f}, "
      f"рагов у молодых {y[ok].mean():.1%} → после отсечения {y[ok & (oof < thr)].mean():.1%}")
clf = mk().fit(X[:, keep], y)
out = pathlib.Path(rugmodel.PATH)
out.parent.mkdir(exist_ok=True)
with open(out, "wb") as fh:
    pickle.dump(dict(clf=clf, keep=keep, threshold=thr, n=len(S), rugs=int(y.sum())), fh)
print(f"образцов {len(S)}, рагов {y.sum()}, признаков {len(keep)}, порог {thr:.4f} → {out}")
