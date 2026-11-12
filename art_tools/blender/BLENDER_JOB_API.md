# Claude Blender 作业接口 v1

本文对应 `prepare_batch.py` 与 `blender_job_runtime.py` 的实际接口，只说明执行和交付合同。建筑、院落、道具的造型、修改方法和布局由真实 Claude 根据本轮任务书及参考决定。不要把旧工具中的院落或屋顶验证稿当作必须采用的生产方案。

只编辑可信批次配置指定的 `recipe_file` 与 `blender_job_file`。通过控制器提供的固定 `preview` 命令运行。不要修改可信配置、框架、本文、账本或密钥，不自行提交 Git。控制器冻结作业快照并记录 SHA；实际执行的是该快照，不是运行途中重新读取的工作区脚本。

作业必须定义 `run_job(context)`，返回恰好含 `instances`、`plots`、`report` 三个键的字典。可以在作业内使用 Blender Python API。全部返回值必须能序列化为有限值 JSON；不能放入 bpy 对象、Vector、Path、NaN 或 Infinity。快照校验是来源追踪，不是 Python 安全沙箱，不得借此访问任务外文件或服务。

有 `blender_job` 时，框架只导入三户源模型，应用已批准的朝向、统一缩放、原点规范和现有贴图技术处理，然后调用作业。它不会运行旧 `roof_repair`、`create_props` 或 `assemble`，不会自动添加院墙、门、道具或布局。

## 坐标与尺度

源对象及建模 API 使用米，Z 向上，约定正面为局部 -Y。住宅在进入作业前已经应用 recipe 的 `front_rotation_deg` 和统一尺度约束；不要重复做整批单位转换。每户当前尺寸可用 `api['bounds']([house])` 测量。

`instances.location_cm` 与 `plots.center_cm` 是世界坐标厘米；`rotation_deg` 是 `[X, Y, Z]` 角度，Z 表示 UE yaw；`scale` 是三个正比例。Blender 展示副本的位置由厘米除以 100，旋转模式固定为 XYZ。资产原点和局部网格必须与实例变换一致。`join_meshes` 会把对象世界变换烘进局部顶点并将对象矩阵归一；导出前需理解这对原点的影响。

固定八镜头要求下列展示协议。它规定展示地块的位置和识别方式，不规定地块内的庭院布局。

| level | plot_id | center_cm | plot_size_m |
| --- | --- | --- | --- |
| L1 | Plot_L1 | [-2000, 0, 0] | [16, 16] |
| L4 | Plot_L4 | [0, 0, 0] | [16, 16] |
| L8 | Plot_L8 | [2000, 0, 0] | [16, 16] |

每个 `instance_id` 必须以所属 `plot_id + '_'` 开头。截图脚本依靠此前缀隐藏其他等级；放错中心或命名会导致截图遗漏。框架会拒绝违反上述协议的输出。实际物体是否完整落在地块内、是否互相穿插，仍需自行测量并查看实际 UE 图。

## context

| 键 | 实际值与用途 |
| --- | --- |
| houses | 按 L1、L4、L8 顺序排列的三个已导入 bpy Mesh 对象；保留原对象及名称，原位编辑其网格和材质。 |
| entries | 输入资产条目列表的副本；包含批准的 level、输入、尺度及朝向等字段。改变该副本不会改变批准范围。 |
| manifest | 本轮生产输入清单的副本，包括源模型、recipe/job 快照和框架依赖信息。 |
| output_dir | 本轮独立输出目录的 `pathlib.Path`。只在此处写本轮附属审计产物。 |
| api | 下列工具名到函数的映射。 |
| assets | 实际导出资产记录 list；通过 `export_asset` 更新。 |
| materials | 实际材质描述登记 dict，键为材质名；通过材质处理函数登记。 |

## api 完整签名

- `bounds(objects) -> (minimum, maximum)`：输入对象列表，返回考虑 `matrix_world` 的两个米制 Vector。
- `join_meshes(objects, name) -> bpy.types.Object`：转换为 Mesh、合并、设名，并烘焙世界变换。活动对象是列表第一项；合并住宅时应保留该住宅作为第一项，避免删除 `houses` 中的对象。
- `export_asset(obj, asset_type=None, provenance=None, extra=None, *, compensate_ue_local_y=False) -> dict`：导出并登记一个 Mesh，返回资产记录。`export_mesh` 是同一受管函数的别名。
- `procedural_material(name, color, roughness, pattern='plain', metallic=0) -> bpy.types.Material`：创建并登记材质及 BaseColor/ORM/Normal 纹理。`color` 是 RGB；现有识别的 pattern 为 plain、wood、cloth、earth、plaster、packed_ground。选用何种材质由作业负责。
- `process_material(source, name, source_normal_convention) -> bpy.types.Material`：从实际源材质节点和贴图生成已登记材质；法线约定使用 `opengl` 或 `directx`。它保留原图来源，按既有 A 材质处理规则生成导出贴图，不能当作原样复制所有节点的函数。
- `box(name, center, size, material, bevel=.012) -> bpy.types.Object`：基础盒体工具；center/size/bevel 使用米。
- `cylinder(name, center, radius, depth, material, rotation=(0,0,0), vertices=16) -> bpy.types.Object`：基础柱体工具；rotation 使用弧度，与 instances 的角度字段不同。
- `timber_between(name, start, end, thickness, material) -> bpy.types.Object`：连接两个米制端点的基础杆件工具。
- `pottery(name, material) -> bpy.types.Object`：现有通用容器工具；不自动导出或摆放。可自行使用 bpy 制作不同形状，不要求使用此工具。

这些几何工具不会自动登记导出。新增对象需要调用 `export_asset`，才可在 instances 中引用。名称须匹配 `[A-Za-z][A-Za-z0-9_]{0,127}`，并稳定、唯一。

## 导出、材质和来源

新资产默认 `asset_type='prop'`，默认来源为本次 Claude job SHA，API 费用为零。也可使用 `courtyard_module` 等已有非 building 类型；不能新增第四户 building。`extra` 用于自有审计或受支持的功能元数据，不得覆盖资产身份、导出路径/哈希、实测 bounds/dimensions、顶点/三角数、材质槽、来源、单位及导出坐标审计等框架字段。

已有三户住宅按同名重新导出时，会替换登记表中该 ID 的旧记录，保留原 Tripo 源文件及其 SHA、building 类型和批准等级。不能用 `provenance` 参数改写原模型来源。记录局部修改应使用自有审计字段或返回 report，不能把修改后的作品伪称为未改的原模型。

作业结束后，框架会再次导出三户住宅的最终网格，即使作业只进行了原位修改。新增物体不会被自动导出。每个最终材质槽必须指向 materials 中已登记的材质；直接创建 bpy 材质后，需调用 `process_material` 登记并将返回材质赋回对象。`procedural_material` 返回值已经登记。不要只手填 descriptor 来掩盖缺失贴图。框架会给纹理及导出文件计算 SHA。

当前没有受管的 `unregister_asset` 接口。不能删除或改名三户住宅，也不能让实例引用已删除记录。assets 是实际 list，直接删除未引用的新资产记录目前不会被身份检查单独禁止，但不会清理已写出的 FBX/纹理或内部对象映射；不要用这种方式实现删除。优先在创建/导出前决定最终对象，或使用同 ID 导出替换。未被 instances 引用的源对象不会出现在 Blender 展示副本中。

框架保存源对象到隐藏 library，并仅根据返回的 instances 创建展示副本。场景中临时工作对象也会被移入隐藏 library，不能依靠直接摆放未登记物体进入最终展示。每个实例从对应已导出的对象复制网格，应用返回的实例变换。

## FBX 坐标补偿

UE 导入固定使用 `convert_scene_unit=True`、`import_uniform_scale=1`，不能再乘 100。已有校准发现 FBX 的局部 Y 手性转换可能影响具有方向性的复合资产；需结合实际正反面截图决定处理方式。

`export_asset(..., compensate_ue_local_y=True)` 仅在临时导出副本中镜像局部 Y、反转面绕序，原 Blender 网格和 UV 不变，并写入 `fbx_local_y_precompensated` 与导出审计。不要同时不加判断地再旋转实例来重复补偿。房屋最终自动重导出会保留该房屋上一次导出记录中的此标记。

`instances.rotation_deg` 是 UE 使用值。只有 Blender 本地展示需要不同旋转时，才另外提供 `blender_rotation_deg`；它只影响 Blender 副本，UE 仍使用 rotation_deg。建议同时写明 `ue_rotation_override_reason` 解释实测原因。框架不会根据造型自动推断门或正面的方向。

## 返回 instances

instances 是非空列表，每条必须包含：

| 字段 | 合同 |
| --- | --- |
| instance_id | 唯一字符串，必须有 `Plot_L1_`、`Plot_L4_` 或 `Plot_L8_` 中对应前缀。 |
| plot_id | 对应已返回地块的固定 ID。 |
| level | 与该地块相同的 L1/L4/L8。 |
| asset_id | 已导出登记的资产 ID；三户住宅各在自己的地块恰好出现一次。 |
| location_cm | 三个有限数，世界厘米坐标。 |
| rotation_deg | 三个有限角度，XYZ，Z 为 yaw。 |
| scale | 三个有限正数。 |

可选 `blender_rotation_deg` 为三个有限角度。其他 JSON 字段会保留，例如 `layout_role`、坐标校准解释；它们不自动产生新行为。不要用 metadata 代替实际几何。

## 返回 plots

plots 必须恰好三条，顺序 L1、L4、L8。每条必须含 `plot_id`、`level`、`house_asset_id`、`plot_size_m`、`center_cm`。前三者绑定对应的批准住宅；尺寸、中心和命名遵守上面的固定展示协议。`house_asset_id` 从当前 houses 的对象名取得，不是 recipe 中的小写资产请求编号。

可附加有限 JSON 字段记录实际量测、入口与活动区域、物件组织、通路或其他解释。框架不会为缺失的庭院字段补造型，也不会自动认定这些说明已经通过视觉验收。

## 可选的 UE 门洞碰撞核验

资产导出时可通过 `extra={'courtyard_enclosure': metadata}` 声明完整静态围护模块。此声明会开启 UE 的 Complex As Simple、BlockAll 静态碰撞和真实门洞/墙身查询，不是装饰标签。metadata 必须为真实几何提供以下字段：

| 字段 | 实际读取的含义 |
| --- | --- |
| gate_center_m | `[x,y]`，资产局部米制入口中心；当前查询只读前两项。 |
| path_width_m | 需要验证的通路净宽，正米数。 |
| gate_clear_width_m | 实测门洞净宽，至少为 path_width_m。 |
| boundary_half_m | 围护边界半宽，米；墙身测试 x 点为资产原点 ± `boundary_half_m * 0.6`。 |

该模块的实例必须零旋转、单位 scale。门洞检查沿 gate y 两侧各 1.8m 执行，球体半径 0.35m；门洞应畅通、两侧前墙应命中该模块本身。全部使用 `trace_complex=False`，不忽略任何 actor，失败即阻止导入。实际布局不符合这些检查位置时，不得填造假的 metadata；如需不同碰撞验证协议，应报告工具缺口。普通资产未声明该字段时仍走原导入碰撞策略，不能据此声称门洞通行已经核实。

## 返回 report 与证据

report 是 JSON 字典，建议记录实际执行的修改、源文件/输入对象身份、修改前后量测、修改范围、UV/材质处理、布局和碰撞核对、明确未解决的问题，以及附属审计文件相对路径。不要声称执行了未调用的步骤，不要把图片看起来合理写成精确几何证明。

框架将 report 原样保存为 `blender_job_report`，将固定快照描述保存为 `blender_job`；两者同时进入 assets_manifest 和 processing_report。job metadata 含 schema_version、source_file、sha256、size_bytes、snapshot。控制器另外核对源文件、依赖、配方、实际 UE 截图及预算。技术通过只表示可以提交用户视觉验收。
