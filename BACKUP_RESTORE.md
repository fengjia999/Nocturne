# Nocturne 完整数据备份与恢复

Dashboard → **Private settings → Memory Backup**。适用于手机浏览器，不需要 Zeabur 终端。

## 导出

「导出完整备份 ZIP」将 `OMBRE_BUCKETS_DIR` 下的记忆 Markdown、Nearfield、状态 JSON、SQLite 数据库打包；SQLite 使用一致性快照以包含已提交的 WAL 修改。

- 导出不包含 `.api_auth.json`（Dashboard 的密码哈希），不包含环境变量、`config.yaml` 或外部目录。独立设置了 `NOCTURNE_NEARFIELD_DIR` 且路径在 `/app/buckets` 外时，需另行备份该目录。
- 根目录中的 `.env`、`.env.*`、`config.yaml` 和 `.api_auth.json` 在恢复时也会保留，不被移走或覆盖。
- 导出和恢复使用相同限制：最多 15,000 个文件、未压缩数据总量不超过 2 GiB、ZIP 不超过 1 GiB、清单不超过 4 MiB。超限导出会报错并清理临时文件，不会提供无法恢复的下载。
- 备份是**未加密 ZIP**，包含完整私人记忆，请自行妥善保存。
- 运行中仍可能有 Markdown/JSON 写入，因此这是尽力保证一致性的在线快照；重要迁移前最好暂时停止模型向 Nocturne 写入，并核对导出日期及文件量。
- 和 `/api/import/upload`（历史聊天分析导入）不是同一功能。

## 恢复

1. 在 Private settings 上传由上述功能导出的 ZIP，确认「准备恢复」。
2. Nocturne 验证格式、文件名单、SHA-256 校验值、数量和大小后，仅暂存备份。**此时线上数据完全不变。**
3. 在 Zeabur 重新启动 Nocturne **服务进程**。下次进程启动时，在初始化 SQLite 之前应用备份。
4. 重新进入 Dashboard 核对记忆。恢复会**替换**原有数据，不会合并或去重。原登录密码、环境变量保持不变。
5. 恢复前的数据会保留在 `/app/buckets/.nocturne-restore/previous/`（下一次成功恢复会替换该副本），这份目录不会打进导出 ZIP。必要时可以在关闭服务的情况下人工恢复。

上传暂存和启动恢复共用进程间文件锁；同一份数据目录只能有一个恢复操作进行。并发请求会明确拒绝，已暂存的备份不会被后续上传覆盖。恢复状态使用同目录临时文件、同步落盘及原子替换；提交前进程中断时，下一次启动依据完整状态回滚，普通写入异常则立即回滚。

**注意**：使用 Zeabur 的「Restart/重启」保证 Python 进程确实重新启动；仅刷新网页不会应用备份。若还有其他进程共享同一 Volume，需停止它们的写入，避免恢复期间并发写入。由于恢复需要至少一份临时副本，请保证 Volume 剩余空间足够。

## API

- `GET /api/backup/status`：查询是否有待恢复备份及上一份副本。
- `GET /api/backup/export`：下载完整数据 ZIP。
- `POST /api/backup/restore`：同源认证的 multipart/form-data，字段 `file` 和 `confirm=RESTORE`，同时需要请求头 `X-Nocturne-Restore: yes`。只暂存，重启时生效。

所有接口需要 Dashboard 登录 Cookie。所有临时文件、恢复控制目录和账号密码哈希都不会进入完整备份。
