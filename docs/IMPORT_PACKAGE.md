# 单个导入包格式

`ios/MiHealthImporter/ImportPackage.schema.json` 是机器可读的唯一契约。

每个包是一个 JSON 对象，包含 `schemaVersion: 1` 和非空 `records` 数组。每个包最多 50,000 条记录；完整迁移应使用目录级导入，而不是手动逐包选择。

每条记录必须有稳定、包内唯一的 `sourceId` 和 `type`。App 会把 `sourceId` 写入 HealthKit 的同步标识，因此重新导入同一条记录不会创建副本。

支持的类型为：

- `heartRate`、`restingHeartRate`：每分钟次数；
- `oxygenSaturation`：0–1 比例；
- `stepCount`、`activeEnergyBurned`、`cyclingDistance`：数量样本；
- `sleep`：带开始/结束时间和睡眠阶段；
- `workout`：带活动类型、时间，可选距离和活动能量。

未知字段、无时区时间、非有限数、无效区间或不符合类型范围的记录都会被拒绝。
