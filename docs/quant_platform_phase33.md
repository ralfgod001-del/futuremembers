# Quant Platform Phase 33

Phase 33 localizes the Web workspace UI to Simplified Chinese.

## New Capabilities

- The Web page declares `lang="zh-CN"`.
- Sidebar controls are now shown in Chinese:
  - configuration
  - run mode
  - AkShare backtest
  - CTP monitor
  - recent runs
- Dynamic UI text is localized:
  - status pills
  - metric labels
  - table headers
  - empty states
  - CTP monitor health labels
  - CTP alert summaries
- Common trading values are shown in Chinese:
  - buy / sell
  - open / close / close today / close yesterday
  - market / limit
  - pending / filled / rejected / canceled
- The frontend font stack now prefers common Simplified Chinese fonts.

## Notes

- API field names and CSV/JSON column keys remain unchanged for compatibility.
- Localization is currently Simplified Chinese only; a runtime language switch can
  be added later if needed.
