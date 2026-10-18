# 项目格式与辅助脚本

## 运行环境

使用当前任务可用的 Python 3.11 或更新版本；需要 Torch 等第三方库时先确认其 wheel 与 Python 版本兼容。可使用现有虚拟环境，或把所需库安装到当前工作目录的 `vendor` 并通过进程级 `PYTHONPATH` 加载。不要把依赖、权重或用户作品复制进技能目录。

- 导出/验证：`mido`。
- 音频候选匹配：`numpy scipy`。
- PDF 证据准备：`pypdfium2 pillow pdfplumber`；不需要 PDF 时无需这些库。
- 音频解码/MP3：可用 FFmpeg 可执行文件，或 `imageio-ffmpeg`。
- 附带采样试听：`numpy tinysoundfont` 和本地 SF2。它不是自动下载器。

缺依赖时只安装所需部分。需要模型的场景由代理选择合适的本地模型，检查其接口与许可；辅助脚本不绑定任何大模型权重。无模型时仍可用频谱/起音分析加谱面约束继续工作。

## 准备证据

```text
python SKILL_DIR/scripts/prepare_inputs.py --score score.pdf --audio performance.m4a --out WORK/evidence
```

输出原始文件哈希、PDF 页面 PNG、可用的矢量对象和未裁去前导静音的单声道 PCM。目录必须不存在，防止覆盖已有证据。脚本不识别音符；从这些证据提取并复核谱面。

## 规范 JSON

所有 ID 是字符串。音高为 MIDI 0–127，C4=60；channel 为零基 0–15，钢琴不使用 GM 鼓通道 9。所有秒数以**原录音起点**为零，拍点以展开后的演奏谱面起点为零；节拍单位始终为四分音符，6/8 等拍号也不改变该单位。

下面是一个可运行的小例子。第二小节全休止，仍在复核清单中：

```json
{
  "schema_version": 1,
  "title": "Piano reconstruction",
  "origin_seconds": 0.2,
  "ticks_per_beat": 1920,
  "tempo_map": [{"beat": 0, "us_per_beat": 500000}],
  "meters": [{"beat": 0, "numerator": 4, "denominator": 4}],
  "key_signatures": [{"beat": 0, "key": "C"}],
  "channels": [
    {"channel": 0, "name": "Piano RH", "program": 0},
    {"channel": 1, "name": "Piano LH", "program": 0}
  ],
  "bars": [{"id": "1", "beat": 0}, {"id": "2", "beat": 4}],
  "reviewed_bars": ["1", "2"],
  "score_heads": [
    {"id": "h1", "page": 1, "bar": "1", "pitch": 60, "channel": 0},
    {"id": "h2", "page": 1, "bar": "1", "pitch": 60, "channel": 0},
    {"id": "h3", "page": 1, "bar": "1", "pitch": 48, "channel": 1}
  ],
  "notes": [
    {"id": "n1", "channel": 0, "pitch": 60, "velocity": 70,
     "onset_seconds": 0.2, "key_release_seconds": 1.18,
     "source": "score", "source_heads": ["h1", "h2"], "source_relation": "tie"},
    {"id": "n2", "channel": 1, "pitch": 48, "velocity": 60,
     "onset_seconds": 0.2, "key_release_seconds": 0.68,
     "source": "score", "source_heads": ["h3"]}
  ],
  "pedal_expected": true,
  "pedal": [{"time_seconds": 0.3, "value": 127}, {"time_seconds": 1.5, "value": 0}],
  "omitted_heads": [],
  "end_audio_seconds": 3.0
}
```

必需含义：

- `score_heads`：从原谱独立清点的**演奏实例**清单；先完成清单，不能为迎合输出计数删头。建议保留 `printed_id`、定位框、谱面拍点/音值、声部和读谱依据等额外字段。
- `bars` 与 `reviewed_bars`：包括反复展开与休止小节；检查完成后才加入 reviewed，不应自动把未看过的小节标成已检查。
- `notes`：实际物理击键。延音线续音和共享音头合并到 `source_heads`；多头映射必填 `source_relation` 为 `tie`、`shared` 或 `tie_and_shared`。脚本只验证映射一致性，不判断连线画法。
- `key_release_seconds`：物理松键，不是声学尾部。额外可保存 `acoustic_offset_seconds`、`release_basis`、置信度和局部复核结果，导出器不会自动使用它们拉长时值。
- `pedal`：一个钢琴的全局 CC64 曲线，自动发送至所有活动通道。事件需单调且在 tick 精度下不同；允许有证据的中间值，不自动生成半踏板。
- `pedal_expected`：音乐确实需要延音踏板时为 true。曲线不应机械复制模板；需要无踏板时可 false 且 `pedal: []`。
- `tempo_map`：从 beat 0 开始，单位为微秒/四分音符。60000000 / BPM 得到该值。速度事件必须能被 TPQ 精确表示，避免累计误差。实际起音在这个图上转换到 tick，保留局部演奏偏移。
- `end_audio_seconds` 可省略，默认为最后松键/抬踏加 2 秒。长衰减应按实际音源/录音加长；不得截掉最后事件或后面的休止小节标记。

特殊情况必须明确：

- 录音额外击键用 `source: "recording"`、`source_heads: []`，并填具体 `evidence`；不能挤占谱面音头的对应。
- 用户目标确实要求改变某谱面音的音高/通道时，在该音符填 `deviation_reason`；输出报告会单列，不视为无差异通过。
- 明确的演奏删音在 `omitted_heads` 填 `{"head_id": "...", "reason": "..."}`；未匹配、低置信度本身不够。覆盖报告单列“有解释的省略”，不能声称这些音仍在 MIDI。
- 同音跨轨重叠被拒绝；同一架钢琴的同键只能有一次物理击键，先在项目中复核并合并共享写法，或在真实再弹前明确松键。

## 导出与验证

```text
python SKILL_DIR/scripts/midi_project.py export WORK/project.json OUTPUT/restored.mid
python SKILL_DIR/scripts/midi_project.py verify WORK/project.json OUTPUT/restored.mid
```

导出器拒绝覆盖已有 MIDI，除非显式加 `--overwrite`。输出旁会生成 `.validation.json` 与 `.coverage.tsv`。它检查每个音头有唯一对应或有说明的省略、每个小节已复核、每次击键的起止、量化误差、全部活动通道的 CC64 曲线、结尾复位和速度图。不要把“结构通过”表述为所有音乐判断均无误。

只修踏板等问题、用户已认可节奏时：

```text
python SKILL_DIR/scripts/midi_project.py export WORK/revised.json OUTPUT/revised.mid --baseline WORK/accepted.json
```

这会要求保留共有音符 ID 的原始起音、速度图和时间原点。有证据需要改起音时先记录具体例外并采用相应局部比较，不要为了通过检查更换所有 ID。

## 音频候选比对

```text
python SKILL_DIR/scripts/match_audio.py WORK/project.json WORK/audio_notes.json WORK/matches.json --window 0.25
```

候选文件支持 `{"notes": [{"pitch": 60, "onset_seconds": 1.2}]}`，也支持 `midi_note` / `onset_time` 字段；仍使用原录音秒数。结果列出一对一匹配、未匹配谱面音与额外录音候选，仅供复核。不会自动删改项目。

## 渲染

```text
python SKILL_DIR/scripts/render_preview.py OUTPUT/restored.mid --sf2 PATH/piano.sf2 --bank 0 --preset 0 --wav WORK/preview.wav --mp3 OUTPUT/preview.mp3
```

`--pedal-mode auto` 为默认；探测原生 CC64，无效时用播放层延迟松键适配器。`--preset` 指 SF2 实际预设，需先确认钢琴位置。检查生成的 `.render.json` 中逐通道开关踏板的结果及文件哈希。脚本针对单一钢琴，不支持多乐器自动配器；MIDI 程序切换不会在预设钢琴音源中重现。

遇到错误先修项目/依赖/音源的具体问题，保存检查证据；不跳过断言交付看似完成的版本。
