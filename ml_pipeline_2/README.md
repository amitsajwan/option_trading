# ml_pipeline_2

Module-local documentation is maintained under [`ml_pipeline_2/docs`](docs/README.md).

Start there for:
- architecture
- detailed design and source inventory
- operator runbooks
- staged training and publish flow

Supported staged manifest:
- [`configs/research/staged_dual_recipe.default.json`](configs/research/staged_dual_recipe.default.json)

Supported default contract:
- `support_dataset = snapshots_ml_flat_v2`
- `stage1_view_id = stage1_entry_view_v2`
- `stage2_view_id = stage2_direction_view_v2`
- `stage3_view_id = stage3_recipe_view_v2`
