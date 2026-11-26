"""Deterministic tools for a future agent: compare, solve, verify, revise.

All results are conditional list-group scenarios, never actual demand estimates.
"""
import heapq
import json
import math
from datetime import date
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

ROOT=Path(__file__).parent

def integer(value,label):
 if type(value)!=int or not 0<=value<=30000:raise ValueError(label+'必须是0至30000的整数')

def validate_rows(rows):
 names=[r['district'] for r in rows]
 if not rows or len(names)!=len(set(names)):raise ValueError('地区为空或重复')
 for r in rows:
  if type(r['population_60plus'])!=int or r['population_60plus']<=0:raise ValueError('老年人口必须是正整数')
  if type(r['listed_basic_beds'])!=int or r['listed_basic_beds']<0:raise ValueError('床位必须是非负整数')
  if r['population_basis']!='户籍':raise ValueError('不接受混用常住人口口径')

def prepare(rows,ratio,acknowledge_scenario):
 if acknowledge_scenario is not True:raise ValueError('必须明确这是名单分组情景，不能解释为实际缺口')
 if type(ratio) not in (int,float) or not math.isfinite(ratio) or not 0<ratio<=.1:raise ValueError('参照比例必须在0至10%之间')
 validate_rows(rows);prepared=[]
 for r in rows:
  target=int((Decimal(r['population_60plus'])*Decimal(str(ratio))).to_integral_value(rounding=ROUND_CEILING))
  prepared.append({**r,'scenario_target':target,'scenario_difference':max(0,target-r['listed_basic_beds'])})
 return prepared

def objective(rows,allocation):
 # An explicit mathematical preference, not an estimated welfare effect.
 return sum((r['scenario_difference']-allocation[r['district']])**2/r['population_60plus'] for r in rows)

def solve(rows,total,max_each,excluded=None,strategy='priority',ratio=.015,acknowledge_scenario=False):
 integer(total,'新增总指标');integer(max_each,'单区上限')
 rows=prepare(rows,ratio,acknowledge_scenario)
 names={r['district'] for r in rows};excluded=[] if excluded is None else excluded
 if not isinstance(excluded,list) or any(not isinstance(k,str) for k in excluded) or set(excluded)-names:raise ValueError('排除地区不在底表中')
 if strategy not in ('priority','equal','population'):raise ValueError('未知策略')
 caps={r['district']:0 if r['district'] in excluded else min(max_each,r['scenario_difference']) for r in rows}
 if total>sum(caps.values()):return {'feasible':False,'requested':total,'max_feasible':sum(caps.values()),'reason':'在参照差额、上限与排除条件下无法分完；未自动放宽限制'}
 x={k:0 for k in caps}
 if strategy=='priority':
  heap=[]
  for i,r in enumerate(rows):
   if caps[r['district']]:heapq.heappush(heap,(-(2*r['scenario_difference']-1)/r['population_60plus'],r['district'],i))
  for _ in range(total):
   _,k,i=heapq.heappop(heap);x[k]+=1;r=rows[i]
   if x[k]<caps[k]:heapq.heappush(heap,(-(2*(r['scenario_difference']-x[k])-1)/r['population_60plus'],k,i))
 else:
  weights={r['district']:1 if strategy=='equal' else r['population_60plus'] for r in rows}
  # Integer weighted round-robin subject to identical caps, no hidden remainder.
  for _ in range(total):
   eligible=[k for k in caps if x[k]<caps[k]]
   k=min(eligible,key=lambda k:((x[k]+1)/weights[k],k));x[k]+=1
 return {'feasible':True,'allocation':x,'caps':caps,'strategy':strategy,'total':total,'max_each':max_each,
  'excluded':excluded,'ratio':ratio,'objective':objective(rows,x),
  'scope':'名单分组参照情景，不是实际需求预测、政策达标认定或建设指令'}

def verify(rows,solution):
 if not solution.get('feasible'):return {'valid':False,'reason':'没有可行方案'}
 if not isinstance(solution.get('allocation'),dict) or any(type(v)!=int or v<0 for v in solution['allocation'].values()):return {'valid':False,'reason':'分配值不是非负整数'}
 rr=prepare(rows,solution['ratio'],True);x=solution['allocation'];names={r['district'] for r in rr}
 caps={r['district']:0 if r['district'] in solution['excluded'] else min(solution['max_each'],r['scenario_difference']) for r in rr}
 checks={'districts_match':set(x)==names,'integer_nonnegative':all(type(v)==int and v>=0 for v in x.values()),
  'total_exact':sum(x.values())==solution['total'],'caps_recomputed':solution['caps']==caps,
  'constraints_met':set(x)==names and all(x[k]<=caps[k] for k in names),
  'objective_recomputed':set(x)==names and math.isclose(objective(rr,x),solution['objective'],rel_tol=1e-10,abs_tol=1e-10)}
 if solution['strategy']=='priority' and set(x)==names:
  removable=[(2*(r['scenario_difference']-x[r['district']])+1)/r['population_60plus'] for r in rr if x[r['district']]>0]
  addable=[(2*(r['scenario_difference']-x[r['district']])-1)/r['population_60plus'] for r in rr if x[r['district']]<caps[r['district']]]
  checks['no_improving_single_unit_exchange']=not removable or not addable or min(removable)>=max(addable)-1e-12
 return {'valid':all(checks.values()),'checks':checks,'qualification':'仅检查给定数学目标与限制，不证明现实政策效果'}

def audit_baseline(root=ROOT):
 d=json.loads((root/'data/baseline.json').read_text());p=json.loads((root/'data/policy.json').read_text());validate_rows(d['rows'])
 return {'districts':len(d['rows']),'population_sum':sum(r['population_60plus'] for r in d['rows']),
  'listed_district_basic_beds':sum(r['listed_basic_beds'] for r in d['rows']),'municipal_basic_beds_separate':d['municipal_basic_beds'],
  'scenario_only':True,'real_demand_estimate_ready':False,'policy_source':p['source_url'],'limitations':d['limitations']}

def demo(root=ROOT):
 d=json.loads((root/'data/baseline.json').read_text());rows=d['rows']
 plans={s:solve(rows,1000,300,strategy=s,acknowledge_scenario=True) for s in ['priority','equal','population']}
 revised=solve(rows,600,250,excluded=['静安区'],acknowledge_scenario=True)
 conflicting=solve(rows,1000,200,excluded=['静安区'],acknowledge_scenario=True)
 impossible=solve(rows,1000,10,acknowledge_scenario=True)
 out={'mode':'确定性工具演示，未调用大模型','assumptions':{'total':1000,'max_each':300,'ratio':.015,
  'acknowledge_scenario':True,'objective':'最小化各区参照差额平方除以户籍老年人口之和；这是选择的数学目标，不是福利收益'},
  'audit':audit_baseline(root),'plans':plans,'checks':{s:verify(rows,p) for s,p in plans.items()},
  'revised':revised,'revised_check':verify(rows,revised),'infeasible_case':impossible,'conflicting_revision':conflicting}
 (root/'data/scenario_demo.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'plans_verified':all(x['valid'] for x in out['checks'].values()),'revised_verified':out['revised_check']['valid'],'infeasible_case_rejected':not impossible['feasible'],'allocation':plans['priority'].get('allocation')},ensure_ascii=False,indent=2))

if __name__=='__main__':demo()
