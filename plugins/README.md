# MaiBot 插件存放目录

把每个 MaiBot SDK 插件（含 `plugin.py`、`_manifest.json`）作为一个独立子目录
放在这里。桥（`../mofox_maibot_compat`）启动时会自动扫描本目录下所有子目录
并批量加载。

例如：

```
E:\plugins\1\MofoxForMaibotPulgins\
├── mofox_maibot_compat\        # 兼容桥本体
└── plugins\                    # ← 本目录，放被兼容的 MaiBot 插件
    ├── hello_world_plugin\
    │   ├── plugin.py
    │   └── _manifest.json
    └── weather_plugin\
        └── plugin.py
```

也可以在桥配置中用 `plugin.plugin_path` 指定单个插件目录，或用
`plugin.plugins_root` 指向其他目录。
