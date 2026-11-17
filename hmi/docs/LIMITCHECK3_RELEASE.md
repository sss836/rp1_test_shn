# PLC 上位机 3.0.34~limitcheck3

从 [GitHub Release](https://github.com/sss836/rp1_test_shn/releases/tag/v3.0.34-limitcheck3) 获取
Ubuntu 22.04 或 24.04 的 amd64 安装包，并按 `SHA256SUMS` 验证下载文件。默认平台地址为
`https://10.43.17.214:8443`。全量更新前按工程约定停止测试和旧实例。

源码位于此版本标签。复建时，将 `rp1-test-hmi-limitcheck3-resources.tar.gz` 解压到源码根目录；
该归档仅包含安装包依赖的七套模拟回放、网页预览和已发布的离线电机维护资源。
仍需对应系统的已验证 Python 3.12 和 native SDK 运行库，详见 [本机构建说明](LOCAL_CONTROL_BUILD.md)。
安装包内的源码清单和资源清单保留各文件的 SHA256。

连续回放见 [连续限位检查](CONTINUOUS_LIMIT_REPLAY.md)；配置真实工位和完成现场验收见
[真机整模块限位检查](MODULE_LIMIT_CONTROL.md)。限位方案模板在
`hmi/config/examples/limit-profiles/`，需要按实际工装填写和核定。
