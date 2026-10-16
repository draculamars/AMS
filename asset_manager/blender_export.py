"""Executed only in a separate Blender/bpy process, never by the web interpreter."""
import sys
import json
from pathlib import Path
import bpy

args=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:]
source,out,metadata,project=map(Path,args[-4:])
bpy.context.preferences.filepaths.use_scripts_auto_execute=False
bpy.ops.wm.open_mainfile(filepath=str(source),load_ui=False,use_scripts=False)
dependencies=[]
for raw in bpy.utils.blend_paths(absolute=False,packed=False):
    absolute=Path(bpy.path.abspath(raw))
    dependencies.append({'reference':raw,'exists':absolute.exists(),'path':str(absolute.relative_to(project)) if absolute.is_relative_to(project) else raw})
objects=[{'name':o.name,'type':o.type,'parent':o.parent.name if o.parent else None,'visible':o.visible_get(),'matrix_world':[list(row) for row in o.matrix_world]} for o in bpy.context.scene.objects]
bpy.ops.export_scene.gltf(filepath=str(out),export_format='GLB',export_apply=True,export_extras=True,use_visible=True,export_yup=True)
metadata.write_text(json.dumps({'converter':'Blender '+bpy.app.version_string,'scene':bpy.context.scene.name,'unit_system':bpy.context.scene.unit_settings.system,'unit_scale':bpy.context.scene.unit_settings.scale_length,'objects':objects,'dependencies':dependencies,'missing_dependencies':[d for d in dependencies if not d['exists']],'auto_execution':False},ensure_ascii=False))
