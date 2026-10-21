# 文献评价人工标注

以下为正式质量评价协议，评价结果需如实标明完成范围。单人软件模块交接可先完成接口、自动测试和复现文档，将此实验列为待办；不要求其他成员现在参与开发，也不能据此声称已完成正式评价。

这里保存标注规范，不把模型生成内容当作人工标准答案。含论文原文的个人标注表先保存在 Git 仓库外；确定共享范围后再由小组提交。

1. 冻结检索快照、精读论文和阅读文本；单阅读员与多角色组使用相同输入、模型和预算上限。
2. 人工从原文独立整理每篇约 6–8 条关键信息：方法、数据集、指标、主要结果、局限、代码可得性。每条 fact 包含 fact_id、paper_id、field、statement、source_loc、短 quote 和 reviewer。
3. 至少两名成员在不显示组别标签的情况下独立标注。模型输出的 candidate_text 只是待评内容，不是事实清单。模板不包含角色、组别或工作区，条目按不透明 ID 打散。记录分歧与复核结果；汇总工具读取最终仲裁版本。
4. judgments 每条保留不透明 item_id，填写 verdict（correct/incorrect）、covered_fact_ids、reviewer、note。暂未核对保留 pending；incorrect 不能覆盖任何 fact。
5. 完整冻结事实清单后人工设置 gold_scope_complete=true；adjudication 记录两名独立评审、分歧解决状态与复核记录位置。工具会校验事实引用确实存在于相同冻结输入中；语义支持仍由人工判定。
6. 未全部标注时 semantic_accuracy 保持 null，另报已评条目准确率和标注覆盖率；没有完整事实清单时 key_information_recall 保持 null，不能填 0 或编造结果。

免费生成待标注模板（输出路径必须不存在）：

```powershell
python -m scripts.literature.evaluation --workspace "<run-directory>" --init-gold --output "<outside-repo>/human_annotations.json"
```

免费计算指标：

```powershell
python eval/metrics/literature_compare.py --workspace "<single-run>" --workspace "<multi-run>" --gold "<outside-repo>/adjudicated.json" --output "<outside-repo>/comparison.json"
```

没有 --gold 时只计算自动指标，人工质量指标保持空值。完整设计是 3 个问题实例 × 两组 × 每组两次；小规模试点必须注明规模，不能当成正式完整实验。工具不启动模型调用。
