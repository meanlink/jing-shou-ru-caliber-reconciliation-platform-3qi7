import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const weekly={
 list:()=>api('/weekly/reports'),
 create:data=>api('/weekly/reports',{method:'POST',body:JSON.stringify(data)}),
 toggle:id=>api('/weekly/reports/'+id+'/toggle',{method:'POST'}),
 run:id=>api('/weekly/reports/'+id+'/run',{method:'POST'}),
 preview:data=>api('/weekly/preview',{method:'POST',body:JSON.stringify(data)}),
 deliveries:()=>api('/weekly/deliveries'),
 reasons:id=>api('/weekly/deliveries/'+id+'/reasons'),
 recheck:id=>api('/weekly/deliveries/'+id+'/recheck',{method:'POST'}),
 downloadUrl:id=>'/api/weekly/deliveries/'+id+'/download',
};
