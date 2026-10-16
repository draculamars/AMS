import {build} from 'esbuild';
import {mkdir,copyFile,readFile,writeFile} from 'node:fs/promises';
const dest='asset_manager/static/vendor';await mkdir(dest,{recursive:true});
await build({entryPoints:['frontend/model-viewer.js'],outfile:'asset_manager/static/model-viewer.js',bundle:true,format:'esm',minify:true,target:['es2022'],legalComments:'eof'});
for(const name of ['occt-import-js.js','occt-import-js.wasm'])await copyFile('node_modules/occt-import-js/dist/'+name,dest+'/'+name);
await copyFile('node_modules/three/LICENSE',dest+'/THREE-LICENSE.txt');
await copyFile('node_modules/occt-import-js/LICENSE.md',dest+'/OCCT-IMPORT-LICENSE.md');
for(const name of ['license.occt-import-js.txt','license.occt.txt'])await copyFile('node_modules/occt-import-js/dist/'+name,dest+'/'+name);
await writeFile(dest+'/SOURCES.json',JSON.stringify({three:{version:'0.186.1',source:'https://github.com/mrdoob/three.js'},occt:{version:'0.0.23',source:'https://github.com/kovacsv/occt-import-js',build_instructions:'https://github.com/kovacsv/occt-import-js#how-to-build-on-windows'}},null,2));
