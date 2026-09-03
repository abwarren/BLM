# BLM Phase 2 — ML Plan After Dataset Gate PASS

**Status:** Deferred until `PHASE 1` dataset release gate = `PASS`.

**Purpose:** Define the ML work that begins only after extraction, timestamping, market-line lineage, score/clock integrity, prediction/market separation, and leakage controls have been demonstrated clean.

---

# 1. Core objective

The purpose of ML is **not** to make the historical dataset fit better.

The objective is to determine whether BLM can become **more accurate as the clean sample grows**, while preserving strict out-of-sample integrity and the existing v4-pace-1 baseline.

The model should learn from:

- completed historical games;
- the current live game state;
- historical patterns relevant to the current state;
- market/context information that was actually available at prediction time.

The model must never be allowed to learn from information that would not have been available at the moment the prediction was made.

---

# 2. Hard prerequisites

Do not begin Phase 2 unless Phase 1 reports:

```text
DATASET GATE: PASS
```

In particular, Phase 2 depends on:

- clean score/state observations;
- reliable timestamps;
- reliable game-clock reconstruction;
- preserved raw WS market observations;
- correct checkpoint-market selection;
- clean OLV/CLV separation;
- no future market observations entering earlier checkpoints;
- BLM output restricted to `X.0` / `X.5`;
- market lines restricted to `X.5`;
- actual final totals represented as integers;
- correct distinction between BLM position and market outcome;
- `NO_EDGE` separated from genuine settlement `PUSH`;
- no material prediction leakage.

If any of these becomes uncertain during ML development, stop ML work and return to Phase 1 auditing.

---

# 3. Preserve the existing baseline

The current **v4-pace-1** model is the baseline and must remain available for comparison.

Do not overwrite it.

Every candidate model must be evaluated against the same baseline population and equivalent information availability.

Required comparison structure:

```text
                 CLEAN DATA
                     │
          ┌──────────┴──────────┐
          │                     │
          ▼                     ▼
    v4-pace-1              Candidate ML
     BASELINE                 MODEL
          │                     │
          └──────────┬──────────┘
                     ▼
              SAME OOS GAMES
                     │
                     ▼
             SAME CHECKPOINTS
                     │
                     ▼
              SAME METRICS
```

A candidate does not qualify merely because it performs better in-sample.

---

# 4. Before training: build the modelling dataset

Create an explicit modelling table/view containing one row per legitimate game/checkpoint/model decision point.

Each row must identify:

### Identity

- game_id
- competition/league if legitimately available
- event identity
- checkpoint percentage
- checkpoint timestamp

### Game state known at T

- home score
- away score
- total score
- period
- raw clock
- derived elapsed game minutes
- remaining game time
- current pace
- recent scoring rate where enough observations exist
- pace relative to expected pace
- other state features that are demonstrably available at T

### Market state known at T

- checkpoint market line
- market timestamp
- OLV
- CLV only in analyses where its use is explicitly permitted and temporally valid
- market movement available before or at T
- time-of-day features
- other market/context features available at T

### Historical/context features

Features may use completed historical games and prior observations, provided their construction does not include the current game's future information.

### Target

The target must be explicitly defined before training.

Examples may include:

- final total;
- residual/error relative to a legitimate reference;
- probability of the eventual market outcome;
- probability distribution over final totals.

Do not allow the target definition to change silently between experiments.

---

# 5. Feature leakage audit before first model

Before training any candidate model, perform a dedicated leakage audit.

For every feature ask:

```text
Was this value genuinely available at timestamp T?
```

Reject features derived from:

- final score;
- final total;
- future scoring events;
- future market movement;
- later snapshots;
- future checkpoint values;
- closing market information when it was not available at T;
- post-game statistics;
- any target-derived transformation.

Historical aggregate features must also be time-safe.

For example, a historical league average may use games completed **before** the prediction timestamp, but not games that occurred later in the historical timeline.

---

# 6. Time-ordered training and validation

Do not use random train/test splitting for the primary evaluation.

Use chronological / walk-forward validation.

Conceptually:

```text
EARLIER GAMES                         LATER GAMES
─────────────────────────────────────────────────────
TRAIN ───────────────→ TEST
        TRAIN ───────────────→ TEST
                TRAIN ───────────────→ TEST
                        TRAIN ───────────────→ TEST
```

The model may learn from the past and predict the future.

The future may never leak backward into training.

Every fold must record:

- training date range;
- validation date range;
- number of training games;
- number of validation games;
- number of checkpoints;
- feature-generation cutoff;
- model version;
- hyperparameters;
- metrics.

---

# 7. Establish a simple baseline before complex ML

Before testing advanced models, establish a transparent statistical baseline.

Compare at minimum:

1. v4-pace-1;
2. simple historical/statistical baseline;
3. first candidate ML model.

The purpose is to determine whether ML provides genuine incremental information rather than simply fitting noise.

Do not add complexity unless the previous layer demonstrates useful out-of-sample improvement.

---

# 8. Candidate model development

Candidate algorithms may be evaluated after the clean-data gate, but each candidate must be tested under the same walk-forward protocol.

Potential candidates include tree-based regression/classification methods and other appropriate statistical ML models already compatible with the project.

The agent must not select a model solely because it has the best in-sample score.

For each candidate report:

- training performance;
- out-of-sample performance;
- calibration where applicable;
- error distribution;
- performance by checkpoint;
- performance by sample size;
- performance by market state;
- performance by time-of-day where sufficiently sampled;
- performance by pace regime;
- performance relative to v4-pace-1.

---

# 9. Prediction output constraint

The production BLM prediction is a **half-point line/value representation**.

The model must not output arbitrary decimal values into production betting semantics.

Allowed production BLM outputs:

```text
X.0
X.5
```

The conversion/quantization rule must be deterministic, documented, tested, and applied consistently in training/evaluation where appropriate.

Do not use hidden floating-point values in a way that changes the meaning of the displayed or settled BLM prediction.

Maintain the distinction between:

- underlying model score/probability/error estimate, if such a continuous internal quantity is intentionally used;
- production BLM predicted line, which must be `X.0` / `X.5`.

If an internal continuous quantity is retained, it must never silently become the production prediction or market reference.

---

# 10. Model accuracy analysis

Accuracy must be evaluated against the correct target and the correct reference.

For a predicted final total, evaluate prediction error against the **actual final total**.

For a betting decision, evaluate the BLM position against the **market line available at that checkpoint**.

For market settlement, evaluate the **actual final total against that same checkpoint market line**.

Never collapse these into one statistic.

Required conceptual separation:

```text
MODEL ERROR
actual final total vs BLM prediction

BLM EDGE / POSITION
BLM prediction vs checkpoint market

MARKET OUTCOME
actual final total vs checkpoint market
```

These are three different relationships and must remain three different calculations.

---

# 11. Checkpoint-by-checkpoint evaluation

Evaluate candidate models separately at each checkpoint rather than assuming all checkpoints behave identically.

For example:

```text
10%
20%
30%
40%
50%
60%
70%
80%
90%
100%
```

For each checkpoint report:

- sample size;
- mean prediction error;
- median absolute error;
- mean absolute error;
- relevant distribution/error statistics;
- model-vs-market edge distribution;
- market outcome counts;
- BLM position counts;
- NO_EDGE count;
- PUSH count;
- missing/invalid observations.

Do not compare unrelated quantities as though they were the same metric.

---

# 12. Sample-size learning is a first-class analysis

The desired behavior is:

> **As the clean historical sample grows, the model should become more accurate if additional data contains useful signal.**

Do not assume this will happen automatically.

Build learning-curve analysis.

For example, evaluate model performance at increasing training populations:

```text
N = 100
N = 250
N = 500
N = 1,000
N = 2,000
N = 5,000
...
```

Use chronological samples where possible rather than repeatedly randomizing the dataset.

For each sample size measure:

- out-of-sample error;
- variance of error;
- calibration where applicable;
- performance against v4-pace-1;
- performance by checkpoint;
- performance by market state;
- confidence/stability of the estimate.

The purpose is to distinguish:

```text
MORE DATA → MORE SIGNAL
```

from:

```text
MORE DATA → MORE OVERFITTING
```

and:

```text
MORE DATA → NO MATERIAL IMPROVEMENT
```

Do not manufacture improvement by changing evaluation methodology between sample sizes.

---

# 13. Feature discovery and historical analysis before weighting

Before introducing weights, identify which features actually contribute useful out-of-sample information.

Analyze, where sample sizes support it:

- current pace;
- expected pace;
- pace difference;
- pace ratio;
- recent scoring rate;
- score differential;
- quarter/period;
- elapsed game time;
- remaining time;
- checkpoint;
- market line;
- OLV;
- market movement;
- CLV for explicitly post-hoc analysis only;
- time of day;
- game-time bucket;
- league/competition context if valid;
- historical scoring characteristics;
- other features actually captured by the clean dataset.

The purpose is not to assume that a feature is predictive, but to measure whether it contributes incremental out-of-sample information.

---

# 14. Time-of-day and market-context analysis

The clean dataset should support analysis of market behavior and game behavior by time of day.

Examples of useful analysis include:

- game start-hour buckets;
- overnight vs daytime periods;
- market movement behavior by time bucket;
- OLV→checkpoint movement;
- checkpoint→CLV movement;
- model error by time bucket;
- market efficiency/error by time bucket;
- sample size per bucket.

These are hypotheses to test, not facts to encode before sufficient data exists.

Do not hard-code a time-of-day bias merely because a small sample appears to show one.

---

# 15. OLV / CLV / market movement research

Preserve the distinction:

```text
OLV = opening reference

CHECKPOINT MARKET = market actually available at decision time

CLV = closing reference
```

Study:

- OLV → checkpoint movement;
- checkpoint → CLV movement;
- OLV → CLV movement;
- model edge versus market movement;
- whether market movement contains information beyond current game state;
- whether model errors are correlated with market movement.

CLV may be extremely useful for **post-hoc model evaluation and market-efficiency research**, but it must not leak into a live prediction if it was not available at the prediction timestamp.

---

# 16. Error analysis

Do not rely on one aggregate accuracy number.

For every candidate model investigate:

- average error;
- median error;
- absolute error;
- tail errors;
- systematic overprediction;
- systematic underprediction;
- error by checkpoint;
- error by pace regime;
- error by market movement regime;
- error by time-of-day;
- error by game-state regime;
- error as sample size increases.

Look specifically for persistent bias.

Example:

```text
Model systematically +4.2 at 20%
Model approximately unbiased at 50%
Model systematically -2.1 at 90%
```

That is more actionable than a single overall accuracy figure.

---

# 17. Weights are deferred until evidence supports them

**Do not introduce weights simply because the sample size is growing.**

First establish:

1. clean data;
2. baseline performance;
3. walk-forward performance;
4. learning curve;
5. feature contribution;
6. systematic biases;
7. whether different information sources contain independent predictive signal.

Only then evaluate weighting.

Potential weighting questions include:

```text
How much weight should current pace receive?
How much weight should historical scoring rate receive?
How much weight should the market receive?
How much weight should the model receive?
Does weighting improve out-of-sample performance?
Does weighting remain stable as sample size grows?
```

Weights must be learned using training data and validated out-of-sample.

Never tune weights directly on the final evaluation population.

---

# 18. Weight stability analysis

If weighting is eventually introduced, track weight evolution as the dataset grows.

Example:

```text
N=500     weight set A
N=1000    weight set B
N=2000    weight set C
N=5000    weight set D
```

Determine whether weights:

- converge;
- oscillate;
- overreact to small samples;
- become stable after sufficient observations;
- improve out-of-sample performance;
- merely fit historical noise.

A weight is not trusted because it looks intuitive.

A weight is trusted only if it demonstrates stable out-of-sample benefit.

---

# 19. Prevent recursive contamination

Do not allow a model's previous predictions to contaminate the historical target or feature set unless that recursive feature is explicitly designed, timestamped, and generated exactly as it would have been in production.

Avoid accidental circularity such as:

```text
prediction
   ↓
feature
   ↓
new prediction
   ↓
training target
```

The data lineage must remain auditable.

---

# 20. Model versioning

Every candidate model must have a unique version.

Record:

- model version;
- training dataset version;
- training cutoff;
- feature schema version;
- feature list;
- target definition;
- preprocessing version;
- quantization rule;
- hyperparameters;
- training sample count;
- validation folds;
- test population;
- results;
- baseline comparison.

Never overwrite the baseline or silently replace a production model.

---

# 21. Promotion gate

A candidate model may only replace the existing baseline after demonstrating a meaningful out-of-sample advantage under the same evaluation methodology.

Promotion should require:

- clean feature lineage;
- no leakage;
- walk-forward validation;
- sufficient sample size;
- improvement over v4-pace-1;
- stability across multiple temporal folds;
- no unacceptable degradation at important checkpoints;
- prediction-output invariant preserved;
- reproducible training;
- reproducible evaluation.

A candidate that wins only on one small period should not automatically replace the baseline.

---

# 22. Recommended ML execution order

```text
DATASET GATE = PASS
        ↓
FREEZE DATASET VERSION
        ↓
BUILD MODELLING VIEW
        ↓
FEATURE LEAKAGE AUDIT
        ↓
DEFINE TARGET
        ↓
ESTABLISH v4-pace-1 BASELINE
        ↓
ESTABLISH SIMPLE STATISTICAL BASELINE
        ↓
WALK-FORWARD VALIDATION
        ↓
FIRST ML CANDIDATE
        ↓
COMPARE OOS
        ↓
ERROR / BIAS ANALYSIS
        ↓
CHECKPOINT ANALYSIS
        ↓
LEARNING CURVE / SAMPLE-SIZE ANALYSIS
        ↓
FEATURE CONTRIBUTION ANALYSIS
        ↓
OLV / MARKET / CLV / TIME-OF-DAY ANALYSIS
        ↓
IDENTIFY STABLE SIGNALS
        ↓
ONLY THEN TEST WEIGHTS
        ↓
WEIGHT STABILITY ANALYSIS
        ↓
COMPARE AGAINST v4-pace-1
        ↓
PROMOTION GATE
```

---

# 23. First ML agent directive after Phase 1 PASS

Use this as the first directive when the clean-data gate has passed:

```text
PHASE 2 — ML INITIALIZATION

The Phase 1 clean-data gate has passed.

Do NOT immediately optimize or add weights.

First create a reproducible ML research environment around the
clean dataset.

Requirements:

1. Freeze and version the exact dataset used for the first ML run.
2. Preserve v4-pace-1 as the immutable baseline.
3. Build an explicit modelling view with one row per legitimate
   game/checkpoint decision point.
4. Define the target explicitly.
5. Enumerate every feature and its source column.
6. For every feature identify its information timestamp.
7. Prove that no feature contains information from after the
   prediction timestamp.
8. Implement chronological/walk-forward validation.
9. Implement a simple statistical baseline.
10. Evaluate v4-pace-1 using the exact same out-of-sample periods.
11. Produce the first ML candidate only after the above is complete.
12. Record model version, dataset version, features, target,
    training windows, validation windows, and metrics.
13. Preserve BLM production prediction invariant: X.0/X.5.
14. Keep BLM prediction, checkpoint market, OLV, CLV, and actual
    final total as separate fields.
15. Do not introduce adaptive weights yet.
16. Do not promote any model.

Return the research setup and leakage audit first.
STOP before model optimization.
```

---

# 24. Second ML directive — first candidate

```text
PHASE 2 — FIRST ML CANDIDATE

Using the frozen clean dataset and approved modelling view:

1. Train the simplest defensible candidate model.
2. Use chronological training/validation only.
3. Preserve v4-pace-1 as the baseline.
4. Do not tune against the final evaluation population.
5. Record all hyperparameters.
6. Record every training/validation date range.
7. Evaluate out-of-sample.
8. Compare prediction error against actual final totals.
9. Separately evaluate BLM position against checkpoint market.
10. Separately evaluate market outcome against checkpoint market.
11. Break results down by checkpoint.
12. Measure systematic overprediction/underprediction.
13. Produce error distributions rather than only one accuracy number.
14. Do not add weights yet.
15. Do not promote the model.

Return results and STOP.
```

---

# 25. Third ML directive — sample-size learning

```text
PHASE 2 — SAMPLE-SIZE LEARNING CURVE

Determine whether model performance improves as clean historical
sample size grows.

Use chronologically valid training populations.

For multiple increasing sample sizes calculate:

- training sample count;
- out-of-sample sample count;
- model error;
- absolute error;
- bias;
- checkpoint performance;
- performance against v4-pace-1;
- variance/stability.

Do not change evaluation methodology between sample sizes.
Do not randomly reshuffle time-series data to manufacture a learning
curve.

Determine whether increasing sample size produces:

A. genuine improvement
B. diminishing returns
C. no improvement
D. instability/overfitting

Do not add adaptive weights.
Do not promote the model.

STOP after the analysis.
```

---

# 26. Fourth ML directive — feature and bias research

```text
PHASE 2 — FEATURE / BIAS RESEARCH

Using only out-of-sample results, identify which features contribute
incremental predictive information.

Analyze:

- current pace;
- expected pace;
- pace difference;
- pace ratio;
- recent scoring rate;
- score state;
- elapsed time;
- remaining time;
- checkpoint;
- checkpoint market;
- OLV;
- market movement available by T;
- time of day;
- historical/context features.

For each feature determine:

- availability timestamp;
- leakage status;
- relationship to error;
- incremental contribution;
- stability across folds;
- stability as sample size grows.

Identify systematic model bias.

Do not convert correlations into production rules automatically.
Do not add weights yet.

STOP after the evidence report.
```

---

# 27. Fifth ML directive — weighting research

Only issue this directive after sufficient evidence shows that weighting is warranted.

```text
PHASE 2 — ADAPTIVE WEIGHTING RESEARCH

Weights are now being investigated because prior out-of-sample
analysis has identified potentially independent predictive signals.

Do NOT tune weights on the final test population.

For each proposed weighting scheme:

1. fit weights using training data only;
2. evaluate on future validation data;
3. repeat through walk-forward folds;
4. measure performance against v4-pace-1;
5. measure weight stability as sample size increases;
6. test sensitivity to small changes in training population;
7. test whether apparent improvement survives later periods;
8. reject unstable or overfit weights.

Report:

- learned weights;
- training sample size;
- validation sample size;
- temporal fold;
- out-of-sample improvement;
- weight variance;
- degradation periods;
- confidence/stability evidence.

Do not promote the weighting scheme automatically.
STOP after the research report.
```

---

# 28. Final ML philosophy

The desired evolution is:

```text
MORE CLEAN DATA
      ↓
BETTER ESTIMATION
      ↓
MORE STABLE PARAMETERS
      ↓
BETTER OUT-OF-SAMPLE PREDICTIONS
```

Not:

```text
MORE DATA
      ↓
MORE COMPLEXITY
      ↓
MORE WEIGHTS
      ↓
BETTER BACKTEST
```

The model must earn every additional layer of complexity through measurable out-of-sample improvement.

**The clean dataset is the foundation. The model is downstream of the data, not a mechanism for repairing the data.**
