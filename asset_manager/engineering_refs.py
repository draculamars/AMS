"""Read build-script path expressions without importing or executing user code."""
import ast
import fnmatch
import posixpath
from pathlib import PurePosixPath
from . import db

def script_references(asset,text):
    tree=ast.parse(text);env={'__file__':PurePosixPath(asset['relpath'])};found=set();dynamic=0
    def query_files(folder,pattern='*',recursive=False):
        folder=posixpath.normpath(str(folder))
        if folder.startswith(('/','../')) or folder=='..':return []
        prefix='' if folder=='.' else folder+'/'
        with db.connect() as c:
            rows=c.execute('SELECT relpath FROM assets a WHERE project_id=? AND substr(relpath,1,?)=? AND (present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id)) ORDER BY relpath LIMIT 2001',(asset['project_id'],len(prefix),prefix)).fetchall()
        if len(rows)>2000:return []
        return [PurePosixPath(r[0]) for r in rows if fnmatch.fnmatch(r[0][len(prefix):] if recursive else PurePosixPath(r[0]).name,pattern) and (recursive or '/' not in r[0][len(prefix):])]
    def evaluate(node,values):
        if node is None:return None
        if isinstance(node,ast.Constant):return node.value if isinstance(node.value,(str,int,float,bool,type(None))) else None
        if isinstance(node,ast.Name):return values.get(node.id)
        if isinstance(node,(ast.List,ast.Tuple)):return [evaluate(n,values) for n in node.elts]
        if isinstance(node,ast.JoinedStr):
            parts=[evaluate(n.value,values) if isinstance(n,ast.FormattedValue) else evaluate(n,values) for n in node.values]
            return ''.join(str(p) for p in parts) if all(p is not None for p in parts) else None
        if isinstance(node,ast.BinOp):
            left,right=evaluate(node.left,values),evaluate(node.right,values)
            if isinstance(node.op,ast.Div) and isinstance(left,PurePosixPath) and isinstance(right,(str,PurePosixPath)):return left/right
            if isinstance(node.op,ast.Add) and isinstance(left,str) and isinstance(right,str):return left+right
        if isinstance(node,ast.Attribute):
            value=evaluate(node.value,values)
            if isinstance(value,PurePosixPath) and node.attr in {'parent','parents','name','stem','suffix'}:return getattr(value,node.attr)
        if isinstance(node,ast.Subscript):
            value,index=evaluate(node.value,values),evaluate(node.slice,values)
            if isinstance(index,int) and value is not None:
                try:return value[index]
                except (IndexError,TypeError,KeyError):return None
        if isinstance(node,ast.Call):
            if isinstance(node.func,ast.Name) and node.func.id in {'Path','str','int'} and node.args:
                value=evaluate(node.args[0],values)
                if value is None:return None
                if node.func.id=='Path' and isinstance(value,(str,PurePosixPath)):return PurePosixPath(value)
                if node.func.id=='str':return str(value)
                if node.func.id=='int' and isinstance(value,(int,float)):return int(value)
            if isinstance(node.func,ast.Attribute):
                base=evaluate(node.func.value,values);name=node.func.attr
                if isinstance(base,PurePosixPath):
                    if name in {'resolve','absolute','expanduser'}:return base
                    if name=='joinpath':
                        args=[evaluate(x,values) for x in node.args]
                        if all(isinstance(a,(str,PurePosixPath)) for a in args):return base.joinpath(*args)
                    if name in {'glob','rglob'} and node.args:
                        pattern=evaluate(node.args[0],values)
                        if isinstance(pattern,str) and '..' not in pattern:return query_files(base,pattern,name=='rglob')
        return None
    def dotted(node):
        if isinstance(node,ast.Name):return node.id
        if isinstance(node,ast.Attribute):return dotted(node.value)+'.'+node.attr
        return ''
    def record(value,kind,directory=False):
        nonlocal dynamic
        if value is None:dynamic+=1;return
        if not isinstance(value,(str,PurePosixPath)):return
        # Absolute host paths remain explicit external references.
        path=posixpath.normpath(str(value))
        if path.startswith('/'):
            found.add((path,kind));return
        if path.startswith('../') or path=='..':return
        if directory:
            for item in query_files(path,'*',True):found.add(('project://'+str(item),kind))
        else:found.add(('project://'+path,kind))
    def calls(node,values):
        for n in ast.walk(node):
            if not isinstance(n,ast.Call):continue
            name=dotted(n.func);last=name.rsplit('.',1)[-1]
            kw={k.arg:k.value for k in n.keywords}
            if isinstance(n.func,ast.Attribute) and last in {'read_text','read_bytes','write_text','write_bytes'}:
                base=evaluate(n.func.value,values)
                if isinstance(base,PurePosixPath):record(base,'build_input' if last.startswith('read') else 'build_output')
            elif name in {'shutil.copytree','shutil.copy2','shutil.copyfile'} and len(n.args)>=2:
                record(evaluate(n.args[0],values),'build_input',name.endswith('copytree'))
                if not name.endswith('copytree'):record(evaluate(n.args[1],values),'build_output')
            elif 'importers.import' in name or last in {'load_mesh','load_meshes'}:
                if n.args:record(evaluate(n.args[0],values),'build_input')
            elif 'exporters.export' in name and len(n.args)>=2:record(evaluate(n.args[1],values),'build_output')
            elif name.startswith('bpy.ops.') and 'filepath' in kw:
                kind='build_output' if any(x in name for x in ['save_as','export_','export.']) else 'build_input'
                record(evaluate(kw['filepath'],values),kind)
    def statements(nodes,values,depth=0):
        if depth>8:return
        for node in nodes:
            if isinstance(node,ast.Assign):
                value=evaluate(node.value,values)
                for target in node.targets:
                    if isinstance(target,ast.Name):values[target.id]=value
                calls(node.value,values)
            elif isinstance(node,ast.For):
                items=evaluate(node.iter,values)
                if isinstance(items,list) and len(items)<=500 and isinstance(node.target,ast.Name):
                    for item in items:statements(node.body,{**values,node.target.id:item},depth+1)
                else:statements(node.body,dict(values),depth+1)
            elif isinstance(node,ast.If):
                # Both branches are references, not executed control flow.
                statements(node.body,dict(values),depth+1);statements(node.orelse,dict(values),depth+1)
            elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):statements(node.body,dict(values),depth+1)
            else:calls(node,values)
    statements(tree.body,env)
    return sorted(found),dynamic
