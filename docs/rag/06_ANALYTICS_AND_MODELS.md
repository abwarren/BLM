# Analytics and Model Evaluation

BLM analytics must be grounded in actual historical market observations and settled sporting outcomes.

## Evaluation rules
- Use settled games for outcome-based performance reporting.
- State sample size, date range, league and inclusion criteria.
- Separate missing/incomplete records from valid observations.
- Report MAE, RMSE, bias and other metrics with their exact definitions.
- Compare predictions to the relevant market reference when evaluating market-line value; do not substitute a model's own prediction for the market line.
- Prevent look-ahead bias: only use information available at the prediction timestamp.
- Avoid leakage between training, validation and test datasets.
- Distinguish theoretical model performance from realized betting results.

## Reporting
Every metric should be traceable to its source rows, calculation, filters and code version. State uncertainty and limitations. Do not present a small or biased sample as conclusive evidence.

Do not change model weights, thresholds or strategy rules merely to improve historical metrics without a documented hypothesis and out-of-sample validation.
