# 构建与分发

[文档首页](index.md) · [开发与验证](development.md) · [用户安装说明](installation.md)

面向发布维护者，命令在源码仓库根目录运行。发行包安装用户无需 uv；构建命令只生成本地产物，不上传发布。示例中的 `<版本>` 需替换为 `pyproject.toml` 的实际版本。

## 构建独立发行版与更新依赖

维护者需要完整 Python 3.11+ 和 uv。普通用户运行发行包安装器无需 uv。

```bash
# 重新解析并更新 Python 版本及带哈希的 pip 清单（默认执行升级）
python3 scripts/lock_dependencies.py
# 仅检查 pyproject、uv.lock 与四个 pip 清单是否一致
python3 scripts/lock_dependencies.py --check
# 修改 dependencies/node/package.json 后更新 npm 锁文件
npm install --package-lock-only --ignore-scripts --prefix dependencies/node
# 构建并校验发行包；可用 --uv 指定 uv 的完整路径
python3 scripts/build_release.py
```

每次发布先更新 `pyproject.toml` 的版本号并重新生成锁文件。构建前检查 Python 锁文件一致性；构建器使用临时隔离环境和固定构建依赖，输出 `dist/repo_agent-<版本>-py3-none-any.whl`、`dist/repo-agent-<版本>.tar.gz` 及其 `.sha256`。不会上传或发布。压缩包内所有文件放在 `repo-agent-<版本>/` 顶层目录中；直接解压后进入这个目录即可安装。wheel 文件与安装后的版本目录结构不变。

发行包通过明确的文件清单收集源码、默认模板和资源，不复制 `.venv`、`.git`、项目 `.env`、日志或下载缓存。wheel 包含用于独立重建 Docker 镜像的源码资源压缩包。`release.json` 记录版本、wheel 和各文件 SHA256；独立安装先校验，再写入用户版本目录。安装器不搬迁已创建的虚拟环境，以免破坏入口脚本中的绝对路径。

运行单元测试后，可用构建产物执行真实安装验收。该测试联网下载依赖，在临时用户目录安装，删除下载目录后检查启动、配置恢复、doctor、镜像构建上下文和卸载；镜像入口使用模拟 Docker，不构建真实镜像、不调用模型 API：

```bash
REPO_AGENT_TEST_ARCHIVE="$PWD/dist/repo-agent-<版本>.tar.gz" \
  .venv/bin/python -m pytest -q tests/test_release_distribution.py
```


### 统一分发清单

`build_manifest.py` 是分发文件选择与资源路径映射的唯一维护入口，只依赖 Python 标准库。`build_support.py` 负责接入 setuptools；`scripts/build_release.py` 负责构建和组装，两者共用该清单。

- 在已有源码包内增删 `.py` 文件会自动改变分发文件集合；新增顶层包需修改 `PACKAGES`。
- 保持包内相对路径的 JSON、模板等资源，加入 `PACKAGE_RESOURCES`；复制到 wheel 内特定位置的资源，加入 `RESOURCE_FILES` 的来源／目标映射。
- 构建辅助文件在 `BUILD_FILES` 中声明；安装和恢复脚本在 `INSTALL_SCRIPTS` 中声明。wheel/Docker 输入与安装器文件按用途组合，安装脚本不必放进 wheel。

新增、删除模块或调整上述声明后执行：

```bash
python3 build_manifest.py --write
python3 build_manifest.py
.venv/bin/python -m pytest -q tests/test_build_manifest.py tests/test_release_distribution.py
python3 scripts/build_release.py
```

`--write` 自动更新 `MANIFEST.in`、`.dockerignore` 和 `pyproject.toml` 中带标记的 setuptools 配置区块。其他项目配置保持原样。将清单和这些生成文件一起提交；不要单独手改生成文件。默认不带参数只检查，不修改文件。

Docker 过滤文件按实际选中的文件生成精确允许列表，新增 `.py` 文件后也需要重新生成。Dockerfile 通过该列表复制上下文，不再重复列出各个源码包和根目录资源。包内 `.env`、虚拟环境、缓存、日志及未声明的 JSON 等文件不会被顺带收集。

wheel、sdist、发行包构建及源码模式的镜像重建入口都会检查清单与配置一致性，缺少必需文件或出现符号链接会失败。发行包生成前还会比较实际 wheel、内置 Docker 上下文的文件列表和内容；归档写入完成后再验证归档内容。重复构建会清理旧包构建目录，避免已删除模块残留。源码包也按统一清单过滤文件，并保留 setuptools 元数据。

`tests/test_build_manifest.py` 包含实际 wheel 构建、删除模块后重复构建、sdist 解包后重建 wheel、新增 JSON 资源、生成配置漂移和未声明文件排除等回归；默认使用测试环境中已有的 setuptools/wheel，不联网安装构建工具。

`requirements-dev.lock` 从 `pyproject.toml` 的 `dev` extra 导出，包含 pytest、Ruff 及其依赖；源码安装自动使用该清单，发行版安装不使用它。它随统一构建清单提供，不表示普通用户运行环境会安装开发工具。
