importScripts('/static/vendor/occt-import-js.js');
let engine;
self.onmessage=async({data})=>{
 try{engine=engine||await occtimportjs({locateFile:name=>'/static/vendor/'+name});
 const method=data.ext==='.brep'?'ReadBrepFile':['.igs','.iges'].includes(data.ext)?'ReadIgesFile':'ReadStepFile';
 const result=engine[method](new Uint8Array(data.buffer),{linearUnit:'millimeter',linearDeflectionType:'bounding_box_ratio',linearDeflection:0.001,angularDeflection:0.3});
 if(!result.success)throw Error('CAD 文件无法解析');self.postMessage({result});
 }catch(e){self.postMessage({error:e.message||String(e)})}
};
