// Realise dashboard behaviour.
//
// This used to sit inline in dashboard.html, which meant 241 KB of JavaScript
// was re-sent with every single page view. As its own file the browser
// downloads it once and then keeps it (our static files are served compressed
// and marked cacheable for years).
//
// The two values that came from Django are now handed over in window.__CP,
// set by a small inline block in the template just above this file.
/* ===== INIT FROM DJANGO ===== */
window.currentUser = window.loginUser || window.__CP.loginUser;

function getCSRF() {
  return document.querySelector('meta[name="csrf-token"]').content;
}

var API = '/realise';
var CACHE_KEY = 'cp:realise:dashboard-data:v1';
var SC2_CACHE_KEY = 'cp:realise:slide2-data:v1';

/* ===== PRODUCTS LIST ===== */
var PRODUCTS=[
  {u_type:"COMMODITY",u_sub_group:"BLENDED",ts:30000,tr:130},
  {u_type:"COMMODITY",u_sub_group:"COTTON SEED",ts:20000,tr:130},
  {u_type:"COMMODITY",u_sub_group:"MUSTARD",ts:625000,tr:145},
  {u_type:"COMMODITY",u_sub_group:"RICE BRAN",ts:25000,tr:131},
  {u_type:"PREMIUM",u_sub_group:"SLICED OLIVE",ts:0,tr:0},
  {u_type:"COMMODITY",u_sub_group:"SOYABEAN",ts:400000,tr:123},
  {u_type:"COMMODITY",u_sub_group:"SUNFLOWER",ts:135000,tr:145},
  {u_type:"PREMIUM",u_sub_group:"BLENDED",ts:10000,tr:190},
  {u_type:"PREMIUM",u_sub_group:"CANOLA",ts:350000,tr:205},
  {u_type:"PREMIUM",u_sub_group:"COCONUT",ts:5000,tr:449},
  {u_type:"PREMIUM",u_sub_group:"EXTRA VIRGIN OLIVE",ts:15000,tr:500},
  {u_type:"PREMIUM",u_sub_group:"GHEE",ts:15000,tr:536},
  {u_type:"PREMIUM",u_sub_group:"GROUNDNUT",ts:50000,tr:175},
  {u_type:"PREMIUM",u_sub_group:"OLIVE",ts:310000,tr:253},
  {u_type:"PREMIUM",u_sub_group:"SESAME",ts:5000,tr:290},
  {u_type:"PREMIUM",u_sub_group:"YELLOW MUSTARD",ts:10000,tr:180}
];

var savedTargets={};
var savedTargetsLoaded=false;
var updateAuthUser=null;

async function loadSavedTargets(){
  try{
    var monthSel=document.getElementById('fMonth');
    var yearSel=document.getElementById('fYear');
    var monthName=monthSel?monthSel.value:currentMonth();
    var monthNum=MONTHS.indexOf(monthName)+1;
    if(monthNum<1)monthNum=new Date().getMonth()+1;
    var yearVal=yearSel&&yearSel.value?parseInt(yearSel.value,10):new Date().getFullYear();
    if((!yearSel||!yearSel.value)&&monthSel&&monthSel.value&&allData&&allData.length){
      for(var ai=0;ai<allData.length;ai++){
        if(allData[ai].month===monthSel.value&&allData[ai].year){yearVal=parseInt(allData[ai].year,10);break;}
      }
    }
    var res=await fetch(API+'/api/targets/?month='+monthNum+'&year='+yearVal,{headers:{'X-CSRFToken':getCSRF()}});
    if(res.ok){var payload=await res.json();savedTargets=payload.data||{};savedTargetsLoaded=true;}
  }catch(e){console.warn('[TARGETS] Could not load saved targets:',e);}
}

function getDefTS(u_type,u_sub){
  var key=u_type+'|'+u_sub;
  if(savedTargets[key]&&savedTargets[key].tgt_ltrs!==undefined)return savedTargets[key].tgt_ltrs;
  for(var i=0;i<PRODUCTS.length;i++){if(PRODUCTS[i].u_type===u_type&&PRODUCTS[i].u_sub_group===u_sub)return PRODUCTS[i].ts;}
  return 0;
}
function getDefTR(u_type,u_sub){
  var key=u_type+'|'+u_sub;
  if(savedTargets[key]&&savedTargets[key].tgt_rate!==undefined)return savedTargets[key].tgt_rate;
  for(var i=0;i<PRODUCTS.length;i++){if(PRODUCTS[i].u_type===u_type&&PRODUCTS[i].u_sub_group===u_sub)return PRODUCTS[i].tr;}
  return 0;
}

var MONTHS=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
function currentMonth(){return MONTHS[new Date().getMonth()];}

var allData=[],filtered=[],dataFetched=false,isDirty=false,modifiedRealKeys=new Set();
var drillData={},drillOpen={},drillDim={},rootDrillDim='';
var avgRealiseData={},avgRealiseDrill={},avgPeriod='';
var channelRows=[],channelTargetCache={},segmentTargetCache={};
var selectedProducts={};
var currentSlide=0;
// Keep in sync with CHANNEL_MEMBERS in realise/services.py. E-Com / Horeca / CSD are
// their own single-group cards; REST holds the leftover source groups.
var CHANNEL_BLOCKS=[
  {name:'GT', members:['GT']},
  {name:'ROI', members:['ROI']},
  {name:'MT', members:['MT']},
  {name:'ECOM', members:['E-COMMERCE']},
  {name:'HORECA', members:['HORECA']},
  {name:'CSD', members:['CSD']},
  {name:'REST', members:['CASH SALE','CORPORATE','SANGAT','BRANCH','STAFF','REFERENCE','PURCHASE OIL']}
];
// Territory ground truth, supplied by the server (single source = TERRITORY_SHEET).
var TERRITORY_DATA=window.__CP.territory;
var TERRITORY=TERRITORY_DATA.map||{};                 // "GROUP|STATENAME" -> person
// SAP has inconsistent state spellings (CHHATISGARH vs CHHATTISGARH, KERELA vs KERALA,
// JAMMU & KASHMIR vs JAMMU AND KASHMIR, …). Canonicalise state on BOTH the map keys and the
// lookups so an assigned territory still matches the sale regardless of which spelling is used.
var STATE_ALIAS={'CHHATISGARH':'CHHATTISGARH','KERELA':'KERALA','JAMMU & KASHMIR':'JAMMU AND KASHMIR',
                 'ORISSA':'ODISHA','PONDICHERRY':'PUDUCHERRY','UTTARANCHAL':'UTTARAKHAND'};
function canonState(s){ s=String(s||'').trim().toUpperCase(); return STATE_ALIAS[s]||s; }
(function(){var T={};for(var k in TERRITORY){var i=k.indexOf('|');T[i>=0?(k.slice(0,i)+'|'+canonState(k.slice(i+1))):k]=TERRITORY[k];}TERRITORY=T;})();
var ASSIGNED_PERSONS=TERRITORY_DATA.persons||[];       // ordered owners
// Fixed state breakdown per channel (label + the sales-side spellings it matches).
var CHANNEL_STATE_WHITELIST=TERRITORY_DATA.whitelist||{};
// National channels owned by one person regardless of state (E-Commerce, CSD).
var GROUP_OWNERS=TERRITORY_DATA.group_owners||{};
function channelStateLabel(channel,stateName){
  var list=CHANNEL_STATE_WHITELIST[channel];
  if(!list)return String(stateName||'UNKNOWN').trim().toUpperCase()||'UNKNOWN';
  var s=String(stateName||'').trim().toUpperCase();
  for(var i=0;i<list.length;i++){if(list[i].match.indexOf(s)!==-1)return list[i].label;}
  return null; // not in this channel's fixed list
}
function assignedPerson(group,state){
  group=String(group||'').trim().toUpperCase();
  state=canonState(state);
  return TERRITORY[group+'|'+state]||GROUP_OWNERS[group]||null;
}
// Real assigned people (territory owners). A sale with no territory owner falls back to its
// raw SAP salesperson ONLY when that is one of these real people (e.g. TARUN, HAPPY) — not a
// desk/territory label like 'HORECA/INST' or 'PUNJAB GT', which stay under 'Other'.
var REAL_PERSONS={}; (ASSIGNED_PERSONS||[]).forEach(function(p){var k=String(p||'').trim().toUpperCase(); if(k)REAL_PERSONS[k]=1;});
function rawSpPerson(sp){ sp=String(sp||'').trim().toUpperCase(); return REAL_PERSONS[sp]?sp:''; }
// A channel-total target (REST/ECOM) may carry no sales_person. If every other
// target node stored under that channel names the same person, attribute the
// channel total to them too (so it lands on that person, not "Other").
function channelDefaultPerson(channel){
  var ch=String(channel||'').toUpperCase(), owner=GROUP_OWNERS[ch];
  if(owner)return String(owner).toUpperCase();
  var seen=null, ambiguous=false;
  function consider(p){
    p=String(p||'').trim().toUpperCase();
    if(!p)return;
    if(seen===null)seen=p; else if(seen!==p)ambiguous=true;
  }
  try{                                        // persons named on this channel's own target nodes
    for(var i=0;i<detailTargetNodes.length;i++){
      var n=detailTargetNodes[i];
      if(String(n.main_group||'').toUpperCase()===ch)consider(n.sales_person);
    }
  }catch(e){}
  try{                                        // per-state territory owners (a national channel = one person)
    var pre=ch+'|';
    for(var k in TERRITORY){ if(k.indexOf(pre)===0)consider(TERRITORY[k]); }
  }catch(e){}
  return ambiguous?null:seen;                 // ambiguous (many people) → let it fall to "Other"
}
var orderInHandCache=null;
async function fetchOrderInHand(){
  if(orderInHandCache)return orderInHandCache;
  try{
    var res=await fetch(API+'/api/order-in-hand/',{headers:{'X-CSRFToken':getCSRF()}});
    if(!res.ok)return {};
    var payload=await res.json();
    orderInHandCache=payload.data||{};
  }catch(e){orderInHandCache={};}
  return orderInHandCache;
}
for(var pi=0;pi<PRODUCTS.length;pi++)selectedProducts[PRODUCTS[pi].u_type+'|'+PRODUCTS[pi].u_sub_group]=true;
function isProdSelected(type,sub){return selectedProducts[type+'|'+sub]!==false;}

// Visual slide order: the channel/beverages dashboard (internal index 1) is shown FIRST and
// the OILS product table (internal index 0) second. Internal indices are left unchanged so
// all the currentSlide===1 logic (slide-two-mode, channel data load) stays valid; only the
// displayed position, indicator and prev/next direction are remapped.
var SLIDE_ORDER=[1,0];
function slidePos(idx){var p=SLIDE_ORDER.indexOf(idx);return p<0?0:p;}
function setSlide(idx){
  currentSlide=idx<0?0:(idx>1?1:idx);
  var slides=document.querySelectorAll('.rd-slide');
  for(var i=0;i<slides.length;i++)slides[i].classList.toggle('active',i===currentSlide);
  document.body.classList.toggle('slide-two-mode',currentSlide===1);
  var pos=slidePos(currentSlide);
  document.getElementById('slideIndicator').textContent='Slide '+(pos+1)+' / 2';
  document.getElementById('slidePrevBtn').disabled=pos===0;
  document.getElementById('slideNextBtn').disabled=pos===SLIDE_ORDER.length-1;
  // Slide 2 in OILS mode = channel dashboard (needs oils data). In BEVERAGES mode
  // it shows the beverages table instead (handled by applyView).
  if(currentSlide===1 && currentDataset==='oils'){
    ensureSlideTwoDates();
    if(!sc2Fetched && !sc2Loading)loadSlideTwoData();
    else if(sc2Fetched && !sc2Rendered)renderSlideTwo();
  }
  applyView();
}
function ensureSlideTwoDates(){
  var f=document.getElementById('sc2From'), t=document.getElementById('sc2To');
  if(f&&!f.value){var n=new Date();f.value=n.getFullYear()+'-'+String(n.getMonth()+1).padStart(2,'0')+'-01';}
  if(t&&!t.value)t.value=new Date().toISOString().split('T')[0];
  // Seed the remembered Date-Range selection (used to restore it after Month-Wise).
  if(!sc2RangeFrom&&f&&f.value)sc2RangeFrom=f.value;
  if(!sc2RangeTo&&t&&t.value)sc2RangeTo=t.value;
}
function nextSlide(){var p=slidePos(currentSlide);if(p<SLIDE_ORDER.length-1)setSlide(SLIDE_ORDER[p+1]);}
function prevSlide(){var p=slidePos(currentSlide);if(p>0)setSlide(SLIDE_ORDER[p-1]);}

/* ===== OILS / BEVERAGES dataset toggle + Beverages view =====
   Beverages is a separate dataset (JIVO_BEVERAGES_HANADB / REPORT_SALES_COGS).
   When selected it replaces the oils slides with a single dynamic-drill table
   (Variety / Sub-Group / SKU, reorderable) showing Quantity & Boxes. */
var currentDataset='oils';
var BEV_DIMS=[{key:'customer',label:'Customer'},{key:'variety',label:'Variety'},{key:'sub_group',label:'Sub-Group'},{key:'brand',label:'Brand'},{key:'sku',label:'SKU'},{key:'item',label:'Item Name'},{key:'sales_person',label:'Sales Person'},{key:'main_group',label:'Main Group'},{key:'chain',label:'Chain'},{key:'state',label:'State'}];
var BEV_DIM_NAME={variety:'Variety',sub_group:'Sub-Group',brand:'Brand',sku:'SKU',item:'Item Name',sales_person:'Sales Person',main_group:'Main Group',chain:'Chain',state:'State',customer:'Customer'};
var BEV_TAG_CLS={variety:'group',sub_group:'state',brand:'item',sku:'item',item:'state',sales_person:'person',main_group:'person',chain:'person',state:'group',customer:'person'};
// Drill-order presets for the View selector. Customer = by party (default); Main = product
// breakdown; Person = by salesperson.
var BEV_VIEWS={customer:['customer'],main:['variety','sub_group','brand'],person:['sales_person']};
var bevOrder=BEV_VIEWS.customer.slice(), bevChosen=bevOrder.slice();
var bevRows=[], bevFetched=false, bevLoading=false, bevExpanded={}, bevTree=[];
// Inline invoice/SO expansion under a driller value. bevDocState: path+'|'+metric ->
// {open,loading,docs,err}; bevNodeFilters: path -> {dim:value} accumulated down the tree;
// bevRangeStart/End: the date range of the loaded data (so doc queries match it).
var bevDocState={}, bevNodeFilters={}, bevRangeStart='', bevRangeEnd='';
var bevTodayBoxes=0, bevYestBoxes=0;
var bevBrand='', bevTodayItems=[], bevYestItems=[], bevTodayDate='', bevYestDate='';
// The same day span one calendar month back (01-11 Sep -> 01-11 Aug), shown in the
// 'Last Month' card so the selected range has something to be compared against.
var bevPrevBoxes=0, bevPrevItems=[], bevPrevStart='', bevPrevEnd='';
var bevMode='range';   // 'range' = From/To dates, 'months' = last-N-months
var bevMonth='';       // selected YYYY-MM in month-wise view ('' = all months)
var bevCustomerRows=[], bevMonthRows=[], bevTopCustList=[], bevTopMonthList=[];
var bevOihRows=[];   // Order-in-Hand rows for the OIH drill popup
var OIH_DIMS=[{key:'variety',label:'Variety'},{key:'sub_group',label:'Sub-Group'},{key:'item',label:'Item Name'},{key:'customer',label:'Customer'}];
var oihPopOrder=['variety'], oihPopExpanded={};
// Each mode keeps its own fetched dataset + filter state so the two are fully
// independent — switching tabs never bleeds one mode's data/inputs into the other.
var bevCache={range:null, months:null};

function setDatasetButtons(oils){
  var os=document.querySelectorAll('.ds-oils'), bs=document.querySelectorAll('.ds-bev'), i;
  for(i=0;i<os.length;i++)os[i].classList.toggle('active',oils);
  for(i=0;i<bs.length;i++)bs[i].classList.toggle('active',!oils);
}
// The OILS/BEVERAGES toggle picks what SLIDE 2 shows; Slide 1 always stays the oils
// product table. Switching jumps to Slide 2 so the change is visible.
function switchDataset(ds){
  if(ds===currentDataset)return;
  currentDataset=ds;
  setDatasetButtons(ds==='oils');
  var pn=document.getElementById('procName'); if(pn)pn.textContent=ds==='oils'?'REPORT_SALES_ANALYSIS':'REPORT_SALES_COGS';
  if(currentSlide!==1){ setSlide(1); return; }   // setSlide() calls applyView()
  applyView();
  if(ds==='oils'){ ensureSlideTwoDates(); if(!sc2Fetched && !sc2Loading)loadSlideTwoData(); else if(sc2Fetched && !sc2Rendered)renderSlideTwo(); }
}
// Reconcile Slide 2's content with the selected dataset.
function applyView(){
  var showBev=(currentSlide===1 && currentDataset==='beverages');
  var bv=document.getElementById('beveragesView'); if(bv)bv.style.display=showBev?'':'none';
  // The .slides container keeps flex:1 even with hidden children, so it would
  // reserve a blank band above the beverages view — hide it while beverages shows.
  var sl=document.querySelector('.slides'); if(sl)sl.style.display=showBev?'none':'';
  if(showBev){ ensureBevDates(); if(!bevFetched && !bevLoading)loadBeverages(); }
}
function ensureBevDates(){
  var f=document.getElementById('bevFrom'), t=document.getElementById('bevTo');
  var sd=(document.getElementById('dFrom')||{}).value, ed=(document.getElementById('dTo')||{}).value;
  if(f&&!f.value)f.value=sd||(new Date().getFullYear()+'-'+String(new Date().getMonth()+1).padStart(2,'0')+'-01');
  if(t&&!t.value)t.value=ed||new Date().toISOString().split('T')[0];
}
// Snapshot / restore / reset the per-mode dataset + filter state.
function bevSnapshot(){
  return {rows:bevRows, todayBoxes:bevTodayBoxes, yestBoxes:bevYestBoxes,
    todayItems:bevTodayItems, yestItems:bevYestItems, todayDate:bevTodayDate, yestDate:bevYestDate,
    prevBoxes:bevPrevBoxes, prevItems:bevPrevItems, prevStart:bevPrevStart, prevEnd:bevPrevEnd,
    customerRows:bevCustomerRows, monthRows:bevMonthRows, oihRows:bevOihRows,
    fetched:bevFetched, brand:bevBrand, month:bevMonth, expanded:bevExpanded,
    rangeStart:bevRangeStart, rangeEnd:bevRangeEnd};
}
function bevRestore(e){
  bevRows=e.rows; bevTodayBoxes=e.todayBoxes; bevYestBoxes=e.yestBoxes;
  bevTodayItems=e.todayItems; bevYestItems=e.yestItems; bevTodayDate=e.todayDate; bevYestDate=e.yestDate;
  bevPrevBoxes=e.prevBoxes||0; bevPrevItems=e.prevItems||[]; bevPrevStart=e.prevStart||''; bevPrevEnd=e.prevEnd||'';
  bevCustomerRows=e.customerRows; bevMonthRows=e.monthRows; bevOihRows=e.oihRows||[];
  bevFetched=e.fetched; bevBrand=e.brand; bevMonth=e.month; bevExpanded=e.expanded;
  bevRangeStart=e.rangeStart||''; bevRangeEnd=e.rangeEnd||''; bevDocState={};
}
function bevResetData(){
  bevRows=[]; bevTodayBoxes=0; bevYestBoxes=0; bevTodayItems=[]; bevYestItems=[];
  bevTodayDate=''; bevYestDate=''; bevCustomerRows=[]; bevMonthRows=[]; bevOihRows=[];
  bevPrevBoxes=0; bevPrevItems=[]; bevPrevStart=''; bevPrevEnd='';
  bevFetched=false; bevBrand=''; bevMonth=''; bevExpanded={};
  bevRangeStart=''; bevRangeEnd=''; bevDocState={};
}
// Toggle between Date Range and Month-Wise fetch modes. Each mode keeps its own
// dataset, so switching tabs restores that mode's last results (or an empty state).
// Month-wise also swaps the day-sales KPIs (Today/Yesterday) for Top Customers / Top Months.
function setBevMode(m){
  if(m===bevMode)return;
  bevCache[bevMode]=bevSnapshot();   // preserve the mode we're leaving
  bevMode=m;
  var month=(m==='months'), i;
  document.getElementById('bevModeRange').classList.toggle('active',!month);
  document.getElementById('bevModeMonths').classList.toggle('active',month);
  document.getElementById('bevRangeControls').style.display=month?'none':'flex';
  document.getElementById('bevMonthControls').style.display=month?'flex':'none';
  document.getElementById('bevMonthFilterCg').style.display=month?'':'none';
  document.getElementById('bevMonthSep').style.display=month?'':'none';
  var dc=document.querySelectorAll('.bev-day-card'), tc=document.querySelectorAll('.bev-top-card');
  for(i=0;i<dc.length;i++)dc[i].style.display=month?'none':'';
  for(i=0;i<tc.length;i++)tc[i].style.display=month?'':'none';
  if(bevCache[m])bevRestore(bevCache[m]); else bevResetData();   // swap to this mode's data
  bevPopulateBrands();
  bevPopulateMonths();
  renderBeverages();
}
function bevYmd(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
// opts.silent → background auto-refresh: no overlay/button spinner, keeps the user's
// drill-expand state, and stays quiet on transient errors (leaves the current view).
async function loadBeverages(opts){
  var silent=!!(opts&&opts.silent);
  var sd, ed;
  if(bevMode==='months'){
    var n=parseInt(document.getElementById('bevMonths').value,10);
    if(!n||n<1){ if(!silent)showToast('Enter number of months','err'); return; }
    if(n>120)n=120;
    var now=new Date();
    ed=bevYmd(now);
    sd=bevYmd(new Date(now.getFullYear(), now.getMonth()-(n-1), 1));   // 1st of the (n-1)th month back
  }else{
    sd=document.getElementById('bevFrom').value; ed=document.getElementById('bevTo').value;
    if(!sd||!ed){ if(!silent)showToast('Select dates','err'); return; }
  }
  if(bevLoading)return; bevLoading=true;
  var fb=document.getElementById('bevFetchBtn'); if(fb&&!silent){fb.disabled=true;fb.textContent='Loading...';}
  if(!silent)document.getElementById('ov').classList.add('show');
  try{
    var res=await fetch(API+'/api/beverages-data/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify({start_date:sd,end_date:ed,prev:bevMode!=='months'})});
    if(!res.ok){var e=await res.json();throw new Error(e.detail||e.error||'Server error');}
    var result=await res.json();
    bevRows=result.data||[]; bevFetched=true; bevRangeStart=sd; bevRangeEnd=ed;
    if(!silent){bevExpanded={}; bevDocState={};}   // keep expanded rows on auto-refresh
    bevTodayBoxes=result.today_boxes||0; bevYestBoxes=result.yesterday_boxes||0;
    bevTodayItems=result.today_items||[]; bevYestItems=result.yesterday_items||[];
    bevTodayDate=result.today_date||''; bevYestDate=result.yesterday_date||'';
    bevPrevBoxes=result.prev_boxes||0; bevPrevItems=result.prev_items||[];
    bevPrevStart=result.prev_start||''; bevPrevEnd=result.prev_end||'';
    bevCustomerRows=result.customer_rows||[]; bevMonthRows=result.month_rows||[];
    bevOihRows=result.oih_rows||[];
    bevPopulateBrands();
    bevPopulateMonths();
    bevCache[bevMode]=bevSnapshot();   // remember this mode's freshly-fetched data
    renderBeverages();
    arMarkUpdated();        // stamp the freshness indicator (manual or silent auto-refresh)
    showToast((silent?'Auto-refreshed · ':'')+bevRows.length+(silent?' rows':' beverage rows loaded'), silent?'info':'ok');
  }catch(e){ if(!silent){ showToast('Error: '+e.message,'err'); document.getElementById('bevBody').innerHTML='<tr><td colspan="4" style="padding:30px;text-align:center;color:#b91c1c">Error: '+esc(e.message)+'</td></tr>'; } }
  finally{ bevLoading=false; if(fb&&!silent){fb.disabled=false;fb.innerHTML='&#9654; Fetch Data';} if(!silent)document.getElementById('ov').classList.remove('show'); }
}
// Auto-refresh the active beverages view every 30 minutes (only while it's visible and
// already fetched). Re-runs the current mode's query — fresh data, since the server cache
// is 90s. Silent so it doesn't disrupt browsing.
var BEV_REFRESH_MS=30*60*1000, bevRefreshTimer=null;
function bevStartAutoRefresh(){
  if(bevRefreshTimer)return;
  bevRefreshTimer=setInterval(function(){
    if(currentSlide===1 && currentDataset==='beverages' && bevFetched && !bevLoading)loadBeverages({silent:true});
  }, BEV_REFRESH_MS);
}
bevStartAutoRefresh();

// -- Live mode (always on) ---------------------------------------------------
// The dashboard keeps itself current with NO clicking and NO waiting. Every AR_PULSE_MS it
// asks the server for a TINY "pulse" fingerprint of the data (invoice counts/sums + open-order
// counts/qty - not the heavy proc, ~15ms and cached 2s server-side). Only when that fingerprint
// CHANGES does it do a real fresh pull, so an idle hour still costs almost nothing.
//
// There is NO throttle on the fresh pull (AR_HEAVY_MS=0): the moment the fingerprint moves, the
// new data is on screen. What stops a billing burst from stacking pulls is arPulseBusy - one
// beat cannot start while the previous pull is still running, so SAP sees at most one pull at a
// time per tab. If SAP ever feels the load, set AR_HEAVY_MS to 10000 and nothing else changes.
//
// It is ON from the moment the page opens. The pill in the header is a STATUS light, not a
// switch you have to find: it says whether live is running, when the data last changed, and -
// this is the part that used to be missing - WHY it is holding back when it is (unsaved target
// edits, tab in the background, or a pull already running). Clicking it pauses/resumes.
var AR_KEY='cp:realise:live:v2';      // v2: v1 stored an OFF that would otherwise stick forever
var arTimer=null, AR_PULSE_MS=3000,  // how often we ask 'did anything change?'
    AR_HEAVY_MS=0,                   // 0 = no throttle: refresh the instant it changed
    AR_PRIME_MS=400;                 // first check straight after the page opens
var arLastPulse=null, arLastHeavy=0, arPulseBusy=false, arLastUpdate=null;
var arPaused=false, arHold='';        // arHold = plain-English reason we are holding back
function arFmtAgo(ms){ var s=Math.round(ms/1000); if(s<5)return 'just now'; if(s<60)return s+'s ago';
  var m=Math.floor(s/60); if(m<60)return m+'m ago'; var h=Math.floor(m/60); if(h<24)return h+'h ago'; return Math.floor(h/24)+'d ago'; }

// -- The status pill ---------------------------------------------------------
// One function paints the whole thing, so the light can never disagree with what the
// heartbeat is actually doing.
function arRenderUpdated(){
  var btn=document.getElementById('arToggle'), lbl=document.getElementById('arToggleLbl'),
      el=document.getElementById('arUpdated');
  var state = arPaused ? 'paused' : (arHold ? 'hold' : 'live');
  if(btn){
    btn.classList.toggle('on',   state==='live');
    btn.classList.toggle('hold', state==='hold');
    btn.setAttribute('aria-pressed', arPaused?'false':'true');
    btn.title = arPaused ? 'Live updates paused - click to resume'
              : arHold   ? ('Live is on, holding off: '+arHold+'. Click to pause.')
                         : ('Live - checking for new data every '+Math.round(AR_PULSE_MS/1000)+'s. Click to pause.');
  }
  if(lbl) lbl.textContent = arPaused ? 'Paused' : 'Live';
  if(!el) return;
  if(arPaused)      { el.textContent='paused'; }
  else if(arHold)   { el.textContent=arHold; }
  else if(arLastUpdate){ el.textContent=arFmtAgo(Date.now()-arLastUpdate); }
  else              { el.textContent='connecting...'; }
  el.title = arLastUpdate ? ('Data last refreshed at '+new Date(arLastUpdate).toLocaleTimeString())
                          : 'No refresh yet this session';
}
function arMarkUpdated(){   // called on every successful data pull (manual Fetch or live refresh)
  arLastUpdate=Date.now(); arRenderUpdated();
  var el=document.getElementById('arUpdated'); if(el){ el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
  var ctl=document.querySelector('.ar-ctl'); if(ctl){ ctl.classList.remove('flash'); void ctl.offsetWidth; ctl.classList.add('flash'); }
  // A manual Fetch already brought the newest data in. Re-take the fingerprint so the next
  // heartbeat compares against what is on screen now instead of firing a duplicate pull.
  arPrime();
}
function arSaveCfg(){ try{ localStorage.setItem(AR_KEY, JSON.stringify({paused:!!arPaused})); }catch(e){} }
function arStop(){ if(arTimer){ clearInterval(arTimer); arTimer=null; } }
function arStart(){
  arStop();
  arTimer=setInterval(heartbeatTick, AR_PULSE_MS);
  // Take the first fingerprint straight away instead of idling for a full interval, so a
  // change that lands in the first minute is caught on the very next beat.
  setTimeout(function(){ heartbeatTick(); }, AR_PRIME_MS);
}
function arDates(){ var f=document.getElementById('sc2From'), t=document.getElementById('sc2To');
  return {start:(f&&f.value)||'', end:(t&&t.value)||''}; }
async function arFetchPulse(){
  var d=arDates(); if(!d.start||!d.end) return '';
  var res=await fetch(API+'/api/sales-pulse/?dataset='+encodeURIComponent(currentDataset)
    +'&start='+encodeURIComponent(d.start)+'&end='+encodeURIComponent(d.end),{headers:{'X-CSRFToken':getCSRF()}});
  if(!res.ok) return '';
  return ((await res.json())||{}).pulse||'';
}
// Re-baseline quietly: "whatever is on screen now matches this fingerprint".
function arPrime(){ arFetchPulse().then(function(fp){ if(fp) arLastPulse=fp; }).catch(function(){}); }

// Why are we not refreshing right now? Empty string = no reason, we are live.
function arHoldReason(){
  if(document.hidden)        return 'tab in background';
  if(isDirty)                return 'unsaved target edits';
  if(sc2Loading||bevLoading) return 'loading...';
  return '';
}

// One heartbeat: cheap pulse -> refresh only if the fingerprint moved (and the throttle allows).
async function heartbeatTick(){
  arHold = arPaused ? '' : arHoldReason();
  arRenderUpdated();
  if(arPaused || arHold) return;              // held back for a reason the pill is now showing
  if(arPulseBusy) return; arPulseBusy=true;
  try{
    var fp=await arFetchPulse();
    if(!fp) return;
    if(arLastPulse===null){ arLastPulse=fp; return; }      // first sample = baseline only
    if(fp===arLastPulse) return;                           // nothing changed -> stay light
    if(Date.now()-arLastHeavy < AR_HEAVY_MS) return;       // throttle heavy pulls; retry next tick
    arLastHeavy=Date.now();
    if(currentDataset==='beverages'){
      if(bevFetched && !bevLoading){ await loadBeverages({silent:true}); arLastPulse=fp; }
    }else if(dataFetched && !sc2Loading && !isDirty){
      detailOihRowsCache=null;                             // force live Order-in-Hand re-fetch too
      await loadData({source:'slide2', silent:true, force:true});   // bypass the 30-min cache
      arLastPulse=fp;                                      // advance baseline only after a fresh render
    }
  }catch(e){ /* quiet - retry next tick */ }
  finally{ arPulseBusy=false; }
}
function toggleAutoRefresh(){
  arPaused=!arPaused; arSaveCfg();
  if(arPaused){ arStop(); arHold=''; arRenderUpdated(); showToast('Live updates paused','info'); }
  else { arStart(); arRenderUpdated(); showToast('Live updates on - the page refreshes itself when data changes','info'); }
}
function initAutoRefresh(){
  // ON unless this browser explicitly paused it. No click needed on a fresh visit.
  var cfg={}; try{ cfg=JSON.parse(localStorage.getItem(AR_KEY)||'{}')||{}; }catch(e){}
  arPaused = cfg.paused===true;
  arRenderUpdated();
  setInterval(arRenderUpdated, 5000);    // keep the "Xs ago" / reason label honest
  // Coming back to the tab should show current data at once, not up to a full interval later.
  document.addEventListener('visibilitychange', function(){ if(!document.hidden && !arPaused) heartbeatTick(); });
  if(!arPaused) arStart();
}
initAutoRefresh();
function buildBevTree(rows,order,depth,prefix,pfilters){
  if(depth>=order.length)return [];
  var dim=order[depth], map={};
  for(var i=0;i<rows.length;i++){var r=rows[i],v=r[dim]||'—';
    if(!map[v])map[v]={name:v,dim:dim,quantity:0,boxes:0,oih:0,_rows:[]};
    map[v].quantity+=r.quantity||0; map[v].boxes+=r.boxes||0; map[v].oih+=r.oih||0; map[v]._rows.push(r);
  }
  return Object.keys(map).map(function(k){var n=map[k];var path=prefix+'>'+dim+':'+n.name;n.path=path;
      var nf={}; for(var fk in (pfilters||{}))nf[fk]=pfilters[fk]; nf[dim]=n.name; n.filters=nf; bevNodeFilters[path]=nf;
      n.kids=buildBevTree(n._rows,order,depth+1,path,nf);delete n._rows;n.leaf=!n.kids.length;return n;})
    .sort(function(a,b){return b.quantity-a.quantity||String(a.name).localeCompare(String(b.name));});
}
function bevTreeRows(nodes,level){
  var html='';
  for(var i=0;i<nodes.length;i++){var n=nodes[i];
    var hasKids=n.kids&&n.kids.length>0, open=!!bevExpanded[n.path];
    var tw=hasKids?'<span class="bev-tw'+(open?' open':'')+'" data-bevpath="'+esc(n.path)+'">&#9654;</span>':'<span class="bev-tw-empty"></span>';
    var tag='<span class="cd-tag '+(BEV_TAG_CLS[n.dim]||'')+'">'+esc(BEV_DIM_NAME[n.dim]||n.dim)+'</span>';
    // Quantity & Boxes -> invoices (sales); OIH -> open SO numbers. Click toggles inline docs.
    var sOpen=!!(bevDocState[n.path+'|sales']&&bevDocState[n.path+'|sales'].open);
    var oOpen=!!(bevDocState[n.path+'|oih']&&bevDocState[n.path+'|oih'].open);
    var qv='<span class="bev-docval'+(sOpen?' open':'')+'" data-bevpath="'+esc(n.path)+'" data-metric="sales" title="Show invoices">'+fN(n.quantity)+'</span>';
    var bv='<span class="bev-docval'+(sOpen?' open':'')+'" data-bevpath="'+esc(n.path)+'" data-metric="sales" title="Show invoices">'+fN(n.boxes)+'</span>';
    var ov=(n.oih>0)?('<span class="bev-docval'+(oOpen?' open':'')+'" data-bevpath="'+esc(n.path)+'" data-metric="oih" title="Show open SOs">'+fN(n.oih||0)+'</span>'):fN(n.oih||0);
    html+='<tr><td style="padding-left:'+(12+level*18)+'px">'+tw+tag+esc(n.name)+'</td><td class="num">'+qv+'</td><td class="num">'+bv+'</td><td class="num">'+ov+'</td></tr>';
    html+=bevDocRows(n.path,'sales',level)+bevDocRows(n.path,'oih',level);
    if(hasKids&&open)html+=bevTreeRows(n.kids,level+1);
  }
  return html;
}
// Inline breakdown beneath a node, for one metric (sales / oih). Rolls the documents up to
// the customers behind the cell (name + their qty/boxes) — e.g. a Sales Person expands to
// the customers they sold to. When the node is already a specific customer the customer
// roll-up would be a single self-row, so we list that customer's invoices/SOs instead.
function bevDocRows(path,metric,level){
  var st=bevDocState[path+'|'+metric];
  if(!st||!st.open)return '';
  var pad=12+(level+1)*18, noun=(metric==='oih'?'open SOs':'invoices');
  if(st.loading)return '<tr class="bev-doc-row"><td style="padding-left:'+pad+'px">Loading&hellip;</td><td></td><td></td><td></td></tr>';
  if(st.err)return '<tr class="bev-doc-row bev-doc-err"><td style="padding-left:'+pad+'px">Could not load.</td><td></td><td></td><td></td></tr>';
  var docs=st.docs||[];
  var pinnedCust=(bevNodeFilters[path]||{}).customer;
  if(!docs.length)return '<tr class="bev-doc-row"><td style="padding-left:'+pad+'px">No '+(pinnedCust?noun:'customers')+' in range.</td><td></td><td></td><td></td></tr>';
  if(!pinnedCust)return bevCustRows(docs,pad);
  var html='', lbl=(metric==='oih'?'SO':'INV');
  for(var i=0;i<docs.length;i++){var d=docs[i];
    html+='<tr class="bev-doc-row"><td style="padding-left:'+pad+'px"><span class="bev-doc-tag">'+lbl+'</span>'+esc(d.doc_num||'—')+' &middot; '+esc(d.doc_date||'—')+'</td>'
      +'<td class="num">'+fN(d.quantity||0)+'</td><td class="num">'+fN(d.boxes||0)+'</td><td class="num"></td></tr>';
  }
  return html;
}
// Customers behind a cell, aggregated from its documents (qty/boxes summed), highest qty first.
function bevCustRows(docs,pad){
  var map={},order=[];
  for(var i=0;i<docs.length;i++){var d=docs[i],c=d.customer||'—';
    var g=map[c]; if(!g){g=map[c]={name:c,quantity:0,boxes:0};order.push(c);}
    g.quantity+=Number(d.quantity)||0; g.boxes+=Number(d.boxes)||0;
  }
  var groups=order.map(function(c){return map[c];})
    .sort(function(a,b){return b.quantity-a.quantity||String(a.name).localeCompare(String(b.name));});
  var html='';
  for(var j=0;j<groups.length;j++){var g=groups[j];
    html+='<tr class="bev-doc-row"><td style="padding-left:'+pad+'px"><span class="bev-doc-tag">CUST</span>'+esc(g.name)+'</td>'
      +'<td class="num">'+fN(g.quantity)+'</td><td class="num">'+fN(g.boxes)+'</td><td class="num"></td></tr>';
  }
  return html;
}
// Toggle (and lazily fetch) the invoice/SO list under a node value.
function bevToggleDocs(path,metric){
  var key=path+'|'+metric, st=bevDocState[key];
  if(st&&st.open){ st.open=false; renderBeverages(); return; }
  if(st&&st.docs){ st.open=true; renderBeverages(); return; }   // reopen cached
  if(!bevRangeStart||!bevRangeEnd){ showToast('Fetch beverages first','info'); return; }
  bevDocState[key]={open:true,loading:true,docs:null,err:false};
  renderBeverages();
  var filters=bevNodeFilters[path]||{};
  var qs='metric='+encodeURIComponent(metric)+'&start='+encodeURIComponent(bevRangeStart)+'&end='+encodeURIComponent(bevRangeEnd);
  for(var k in filters)qs+='&f_'+encodeURIComponent(k)+'='+encodeURIComponent(filters[k]);
  if(bevBrand)qs+='&f_brand='+encodeURIComponent(bevBrand);
  if(bevMonth)qs+='&f_ym='+encodeURIComponent(bevMonth);
  fetch(API+'/api/beverages-docs/?'+qs,{headers:{'X-CSRFToken':getCSRF()}})
    .then(function(r){return r.ok?r.json():{data:[]};})
    .then(function(p){var s=bevDocState[key]; if(!s)return; s.loading=false; s.docs=p.data||[]; renderBeverages();})
    .catch(function(){var s=bevDocState[key]; if(!s)return; s.loading=false; s.err=true; renderBeverages();});
}
// Rows narrowed to the selected brand and/or month ('' = no filter).
function bevActiveRows(){
  if(!bevBrand&&!bevMonth)return bevRows;
  return bevRows.filter(function(r){return (!bevBrand||r.brand===bevBrand)&&(!bevMonth||r.ym===bevMonth);});
}
// Box total of a day's item list, honouring the brand filter.
function bevDayBoxes(items,brand){var t=0,a=items||[];for(var i=0;i<a.length;i++){if(!brand||a[i].brand===brand)t+=a[i].boxes||0;}return Math.round(t*100)/100;}
/* Realise for one of the day / last-month item lists: invoiced value over boxes, the same
   formula the box table uses. Honours the Brand filter, so the rate always describes the
   boxes shown right above it. Returns 0 when there is nothing to divide by. */
function bevDayRealise(items,brand){
  var v=0,b=0,a=items||[];
  for(var i=0;i<a.length;i++){
    if(brand&&a[i].brand!==brand)continue;
    v+=Number(a[i].value)||0; b+=Number(a[i].boxes)||0;
  }
  return b?(v/b):0;
}
// Paints the small "₹x / box" line under a KPI figure. Blank when there is no rate, so a
// card with no sales shows nothing rather than a misleading zero.
function bevSetRealiseLine(id,items,brand){
  var el=document.getElementById(id); if(!el)return;
  var b=bevDayBoxes(items,brand), r=bevDayRealise(items,brand);
  // Blank only when there are no boxes at all. Boxes that earned nothing show 0.00.
  el.innerHTML=b?('₹'+fNp(r,2)+' <span class="u">realise / box</span>'):'';
  el.title=r?'Invoiced value divided by boxes sold - the same formula as the Total Boxes card':'';
}
// View selector → swap the drill order to a preset (Main / Sales Person).
function bevSetView(view){
  bevOrder=(BEV_VIEWS[view]||BEV_VIEWS.customer).slice();
  bevChosen=bevOrder.slice();
  bevExpanded={}; bevDocState={};
  renderBeverages();
}
// Keep the dropdown truthful: match the current drill order to a preset, else show "Custom".
function bevSyncViewSelect(){
  var sel=document.getElementById('bevView'); if(!sel)return;
  var cur=bevOrder.join('|'), match='';
  for(var k in BEV_VIEWS){ if(BEV_VIEWS[k].join('|')===cur){match=k;break;} }
  if(match){ if(sel.value!==match)sel.value=match; var c=sel.querySelector('option[value="custom"]'); if(c)c.remove(); return; }
  if(!sel.querySelector('option[value="custom"]')){
    var o=document.createElement('option'); o.value='custom'; o.textContent='Custom'; sel.appendChild(o);
  }
  sel.value='custom';
}
/* ── Boxes and realise per pack size ─────────────────────────────
   Realise = invoiced value / boxes sold. That is the same number as
   (unit price x case pack): boxes are quantity / case pack, so the case pack
   cancels and only value / boxes is left - one division instead of two, and no
   rounding of a per-piece price along the way.

   Every pack size that sold gets a line, biggest seller first, so the TOTAL row
   is the same figure the separate Total Boxes card used to show. */
function bevSkuLabel(sku){
  var raw=String(sku||'').trim().toUpperCase();
  if(!raw||raw==='\u2014')return 'NO SIZE';        // SAP item with U_SKU left blank
  // SAP writes the unit inconsistently - '250 MLS', '250 ML', '250ML', '1 LTRS'.
  // Normalise so the same size is never split across two lines.
  var m=raw.replace(/[^0-9A-Z.]/g,'').match(/^(\d+(?:\.\d+)?)(ML|MLS|L|LTR|LTRS|LITRE|LITER)$/);
  if(!m)return raw;
  return m[1]+' '+((m[2]==='ML'||m[2]==='MLS')?'ML':'LTR');
}
/* Collapsed by default: the totals only, so the card is the same height as its
   neighbours. Click it to see the per-size breakdown, click again to close. */
var bevBoxData=null;
function bevRenderBoxTable(rows){
  var acc={};
  for(var i=0;i<rows.length;i++){
    var k=bevSkuLabel(rows[i].sku);
    if(!acc[k])acc[k]={box:0,val:0,vat:0};
    acc[k].box+=Number(rows[i].boxes)||0;
    acc[k].val+=Number(rows[i].value)||0;
    acc[k].vat+=Number(rows[i].vat)||0;
  }
  // Biggest seller first, which also puts the odd small sizes at the bottom.
  var list=Object.keys(acc).map(function(k){return {name:k,box:acc[k].box,val:acc[k].val,vat:acc[k].vat};})
                 .filter(function(r){return r.box!==0||r.val!==0;})
                 .sort(function(a,b){return b.box-a.box||String(a.name).localeCompare(String(b.name));});
  var tb=0,tv=0,tg=0;
  for(var j=0;j<list.length;j++){ tb+=list[j].box; tv+=list[j].val; tg+=list[j].vat; }
  bevBoxData={list:list, boxes:tb, value:tv, vat:tg, rows:rows.slice()};
  bevPaintBoxCard();
}
/* Pack sizes across: one column per size, boxes above realise, TOTAL set apart at the
   end. Reads as a comparison between sizes, which stacked rows make harder. */
function bevRenderSizeStrip(){
  var el=document.getElementById('bevSizeStrip');
  if(!el)return;
  var d=bevBoxData;
  if(!d||!d.list.length){ el.innerHTML=''; return; }   // :empty hides the whole block
  var avg=d.boxes?(d.value/d.boxes):0;
  var head='<tr><th>Box</th>', boxes='<tr><td class="bev-sz-lbl">Total Boxes</td>',
      rz='<tr><td class="bev-sz-lbl">Realise</td>',
      /* Realise WITH GST. The tax is summed from the invoice lines, never taken as a
         percentage: the beverages book carries more than one rate (5% and 40% this
         month), so any flat percentage would be wrong for part of the range. */
      rzg='<tr><td class="bev-sz-lbl">Realise + GST</td>';
  for(var i=0;i<d.list.length;i++){
    var r=d.list[i], rate=r.box?(r.val/r.box):0;
    var tip=r.name+': '+fNp(r.val,2)+' over '+fN(Math.round(r.box*100)/100)+' boxes';
    head+='<th title="'+esc(tip)+'">'+esc(r.name)+'</th>';
    boxes+='<td class="bev-sz-b" title="'+esc(tip)+'">'+fN(Math.round(r.box))+'</td>';
    // Guard on BOXES: boxes that earned nothing have a real realise of 0.00; a dash is
    // reserved for "no boxes, so no rate exists".
    rz+='<td class="bev-sz-v" title="'+esc(tip)+'">'+(r.box?('₹'+fNp(rate,2)):'—')+'</td>';
    var gross=r.box?((r.val+(r.vat||0))/r.box):0;
    var gtip=r.name+' with GST: '+fNp(r.val+(r.vat||0),2)+' over '
            +fN(Math.round(r.box*100)/100)+' boxes (GST '+fNp(r.vat||0,2)+')';
    rzg+='<td class="bev-sz-g" title="'+esc(gtip)+'">'+(r.box?('₹'+fNp(gross,2)):'—')+'</td>';
  }
  /* TOTAL is WEIGHTED - all value over all boxes - never the mean of the size rates, which
     would let a one-box size count as much as a 12,000-box one. */
  var ttip='All sizes: '+fNp(d.value,2)+' over '+fN(Math.round(d.boxes*100)/100)
          +' boxes. Weighted, not the mean of the rates.';
  head+='<th class="is-total">Total</th></tr>';
  boxes+='<td class="bev-sz-b is-total" title="'+esc(ttip)+'">'+fN(Math.round(d.boxes))+'</td></tr>';
  rz+='<td class="bev-sz-v is-total" title="'+esc(ttip)+'">'
     +(d.boxes?('₹'+fNp(avg,2)):'—')+'</td></tr>';
  var gvat=Number(d.vat)||0, gavg=d.boxes?((d.value+gvat)/d.boxes):0;
  var gttip='All sizes with GST: '+fNp(d.value+gvat,2)+' over '+fN(Math.round(d.boxes*100)/100)
           +' boxes. GST '+fNp(gvat,2)+', summed from the invoice lines, not a flat rate.';
  rzg+='<td class="bev-sz-g is-total" title="'+esc(gttip)+'">'
      +(d.boxes?('₹'+fNp(gavg,2)):'—')+'</td></tr>';
  el.innerHTML='<table><thead>'+head+'</thead><tbody>'+boxes+rz+rzg+'</tbody></table>';
}
function bevPaintBoxCard(){
  var el=document.getElementById('bevKpiBoxTable');
  if(!el)return;
  var d=bevBoxData;
  bevRenderSizeStrip();   // same data, so the strip can never disagree with the card
  if(!d||!d.list.length){
    el.innerHTML='<div class="sl">Total Boxes</div><div class="sv">—</div>';
    return;
  }
  /* The realise has to be WEIGHTED - total value over total boxes - not the mean of the
     per-size rates. 250 ML sells far more boxes than 1 LTR, so a plain mean would quietly
     overweight the smallest seller and report a realise nobody actually earned. */
  var avg=d.boxes?(d.value/d.boxes):0;
  el.innerHTML='<div class="sl">Total Boxes</div>'
    +'<div class="sv" title="'+esc(fN(Math.round(d.boxes*100)/100)+' boxes in this selection')+'">'
    +fN(Math.round(d.boxes))+'</div>'
    +'<div class="bev-bx-sub" title="'+esc('Realise = '+fNp(d.value,2)+' over '
      +fN(Math.round(d.boxes*100)/100)+' boxes. Weighted, not the mean of the per-size rates.')+'">'
    // Guard on BOXES: boxes that earned nothing have a real realise of 0.00, and a dash
    // is reserved for "no boxes, so no rate exists".
    +(d.boxes?('₹'+fNp(avg,2)+' <span class="u">realise / box</span>'):'—')+'</div>';
}
/* The Total Boxes card opens the same kind of window as the day cards: the pack-size
   split first, then every item behind it. Built from the rows already on screen, so the
   totals can never disagree with the card. */
function bevBoxRealiseCell(val,box){
  return '<td class="num" title="'+esc(fNp(val||0,2)+' over '+fN(Math.round((box||0)*100)/100)+' boxes')+'">'
        +(box?('₹'+fNp((val||0)/box,2)):'—')+'</td>';
}
function openBevBoxes(){
  if(!bevFetched){showToast('Fetch beverages first','info');return;}
  var d=bevBoxData;
  if(!d||!d.list.length){showToast('Nothing to show yet','info');return;}

  document.getElementById('bevBoxTitle').textContent='Boxes & Realise'+(bevBrand?(' — '+bevBrand):'');
  var f=(document.getElementById('bevFrom')||{}).value, t=(document.getElementById('bevTo')||{}).value;
  var avg=d.boxes?(d.value/d.boxes):0;
  document.getElementById('bevBoxSub').textContent=
    (f&&t?bevFmtRange(f,t)+' · ':'')+fN(Math.round(d.boxes*100)/100)+' boxes · '
    +(d.boxes?('₹'+fNp(avg,2)+' realise / box'):'no realise');

  // Roll the on-screen rows up to item / brand / SKU.
  var im={},order=[];
  for(var j=0;j<d.rows.length;j++){
    var w=d.rows[j], key=(w.item||'—')+'|'+(w.brand||'—')+'|'+(w.sku||'—');
    if(!im[key]){im[key]={item:w.item||'—',brand:w.brand||'—',sku:w.sku||'—',
                          qty:0,box:0,val:0};order.push(key);}
    im[key].qty+=Number(w.quantity)||0;
    im[key].box+=Number(w.boxes)||0;
    im[key].val+=Number(w.value)||0;
  }
  var items=order.map(function(k){return im[k];})
                 .filter(function(x){return x.qty||x.box||x.val;})
                 .sort(function(a,b){return b.box-a.box;});
  var tq=0,tb=0,tv=0,html='';
  for(var k2=0;k2<items.length;k2++){
    var it=items[k2];
    tq+=it.qty; tb+=it.box; tv+=it.val;
    html+='<tr><td>'+esc(it.item)+'</td><td>'+esc(it.brand)+'</td><td>'+esc(it.sku)+'</td>'
         +'<td class="num">'+fN(Math.round(it.qty*100)/100)+'</td>'
         +'<td class="num">'+fN(Math.round(it.box*100)/100)+'</td>'
         +bevBoxRealiseCell(it.val,it.box)+'</tr>';
  }
  if(!items.length){
    html='<tr><td colspan="6" style="padding:34px;text-align:center;color:#7b8794">No items in this selection.</td></tr>';
  }else{
    html+='<tr class="cd-total"><td colspan="3">TOTAL</td><td class="num">'+fN(Math.round(tq*100)/100)+'</td>'
         +'<td class="num">'+fN(Math.round(tb*100)/100)+'</td>'+bevBoxRealiseCell(tv,tb)+'</tr>';
  }
  document.getElementById('bevBoxItemBody').innerHTML=html;
  document.getElementById('bevBoxModal').classList.add('show');
}
function closeBevBoxes(){document.getElementById('bevBoxModal').classList.remove('show');}
function renderBeverages(){
  var body=document.getElementById('bevBody');
  var label=bevOrder.map(function(k){return BEV_DIM_NAME[k];}).join(' › ')||'Dimension';
  document.getElementById('bevFirstCol').textContent=label;
  var lbl=document.getElementById('bevDrillLabel'); if(lbl)lbl.textContent=label||'— none —';
  bevSyncViewSelect();
  var rows=bevActiveRows();
  var tq=0,tb=0,toih=0; for(var i=0;i<rows.length;i++){tq+=rows[i].quantity||0;tb+=rows[i].boxes||0;toih+=rows[i].oih||0;}
  tq=Math.round(tq*100)/100; tb=Math.round(tb*100)/100; toih=Math.round(toih*100)/100;
  document.getElementById('bevKpiQty').textContent=fN(tq);
  document.getElementById('bevKpiOih').textContent=fN(toih);
  // Same filtered rows as the KPIs above, so the table's TOTAL always matches them.
  bevRenderBoxTable(rows);
  document.getElementById('bevKpiToday').textContent=fN(bevDayBoxes(bevTodayItems,bevBrand));
  document.getElementById('bevKpiPrev').textContent=fN(bevDayBoxes(bevPrevItems,bevBrand));
  bevSetRealiseLine('bevRzPrev',bevPrevItems,bevBrand);
  var lt=document.getElementById('bevLblToday'); if(lt)lt.textContent="Today's Sales"+(bevTodayDate?(' · '+bevFmtDate(bevTodayDate)):'');
  var lp=document.getElementById('bevLblPrev');
  if(lp)lp.textContent='Last Month'+(bevPrevStart?(' · '+bevFmtRange(bevPrevStart,bevPrevEnd)):'');
  bevTopCustList=bevAggTop(bevCustomerRows,'customer',bevBrand,bevMonth);
  bevTopMonthList=bevAggTop(bevMonthRows,'ym',bevBrand,bevMonth);
  if(!bevFetched){
    document.getElementById('bevKpiQty').textContent='—'; document.getElementById('bevKpiOih').textContent='—';
    // Nothing fetched yet: same shape as the filled card, just with dashes.
    bevBoxData=null;
    var bxEl=document.getElementById('bevKpiBoxTable');
    if(bxEl)bxEl.innerHTML='<div class="sl">Total Boxes</div><div class="sv">—</div>';
    document.getElementById('bevKpiToday').textContent='—'; document.getElementById('bevKpiPrev').textContent='—';
    var rzp=document.getElementById('bevRzPrev'); if(rzp)rzp.innerHTML='';
    body.innerHTML='<tr><td colspan="4" style="padding:40px;text-align:center;color:#7b8794">'+(bevMode==='months'?'Enter a number of months and click Fetch to load beverages.':'Pick a date range and click Fetch to load beverages.')+'</td></tr>';return;}
  if(!bevOrder.length){body.innerHTML='<tr><td colspan="4" style="padding:40px;text-align:center;color:#7b8794">Select at least one drill dimension.</td></tr>';return;}
  if(!rows.length){body.innerHTML='<tr><td colspan="4" style="padding:40px;text-align:center;color:#7b8794">'+(bevBrand?('No rows for brand "'+esc(bevBrand)+'".'):'No beverage rows in this range.')+'</td></tr>';return;}
  bevNodeFilters={};
  bevTree=buildBevTree(rows,bevOrder,0,'',{});
  var html=bevTreeRows(bevTree,0);
  html+='<tr class="cd-total"><td>TOTAL</td><td class="num">'+fN(tq)+'</td><td class="num">'+fN(tb)+'</td><td class="num">'+fN(toih)+'</td></tr>';
  body.innerHTML=html;
}
/* Brand switch: "Both" plus one button per brand actually present (JIVO / SANO today).
   Built from the data so a new brand needs no code change. bevBrand='' means both, and
   every figure on the page already filters on it - the KPI cards, the size strip, the
   customer table and the drill windows - so one button moves all of them together. */
function bevPopulateBrands(){
  var box=document.getElementById('bevBrandBtns'); if(!box)return;
  var seen={}, list=[];
  for(var i=0;i<bevRows.length;i++){
    var b=bevRows[i].brand||'';
    if(b&&b!=='—'&&!seen[b]){seen[b]=1;list.push(b);}
  }
  list.sort(function(a,b){return String(a).localeCompare(String(b));});
  // A brand that vanished with the date range must not stay selected, or the page would
  // silently show nothing.
  if(bevBrand&&list.indexOf(bevBrand)===-1)bevBrand='';
  // Built as real elements with listeners, not an onclick string: a brand name is SAP text
  // and could carry a quote, which would break an inline attribute.
  box.innerHTML='';
  [''].concat(list).forEach(function(b){
    var btn=document.createElement('button');
    btn.type='button';
    btn.className='bev-brand-btn'+(bevBrand===b?' active':'');
    btn.textContent=b||'Both';
    btn.title=b?('Show '+b+' only'):'Show every brand together';
    btn.addEventListener('click',function(){setBevBrand(b);});
    box.appendChild(btn);
  });
}
function setBevBrand(b){
  bevBrand=b||'';
  bevExpanded={}; bevDocState={};
  bevPopulateBrands();     // repaint so the pressed button shows as active
  renderBeverages();
}
function bevPopulateMonths(){
  var sel=document.getElementById('bevMonthFilter'); if(!sel)return;
  var labels={}, list=[];
  for(var i=0;i<bevMonthRows.length;i++){var r=bevMonthRows[i]; if(r.ym&&!labels[r.ym]){labels[r.ym]=r.label||r.ym;list.push(r.ym);}}
  list.sort(function(a,b){return a<b?1:(a>b?-1:0);});   // most recent month first
  var html='<option value="">All Months</option>';
  for(var j=0;j<list.length;j++)html+='<option value="'+esc(list[j])+'">'+esc(labels[list[j]])+'</option>';
  sel.innerHTML=html;
  if(bevMonth && labels[bevMonth])sel.value=bevMonth; else { bevMonth=''; sel.value=''; }
}
function onBevMonthChange(){ bevMonth=document.getElementById('bevMonthFilter').value; bevExpanded={}; bevDocState={}; renderBeverages(); }
function openBevSold(which){
  if(!bevFetched){showToast('Fetch beverages first','info');return;}
  var prev=(which==='prev');
  var items=(which==='today'?bevTodayItems:(prev?bevPrevItems:bevYestItems))||[];
  var dstr=which==='today'?bevFmtDate(bevTodayDate)
          :(prev?bevFmtRange(bevPrevStart,bevPrevEnd):bevFmtDate(bevYestDate));
  if(bevBrand)items=items.filter(function(it){return it.brand===bevBrand;});
  items=items.slice().sort(function(a,b){return (b.boxes||0)-(a.boxes||0);});
  document.getElementById('bevSoldTitle').textContent=(which==='today'?"Today's Sales"
    :(prev?"Last Month's Sales":"Yesterday's Sales"))+(bevBrand?(' — '+bevBrand):'');
  var tq=0,tb=0,tv=0;
  for(var i=0;i<items.length;i++){tq+=items[i].quantity||0;tb+=items[i].boxes||0;tv+=items[i].value||0;}
  document.getElementById('bevSoldSub').textContent=(dstr?dstr+' · ':'')+items.length+' item(s) · '+fN(Math.round(tb*100)/100)+' boxes';
  var body=document.getElementById('bevSoldBody');
  if(!items.length){
    body.innerHTML='<tr><td colspan="6" style="padding:34px;text-align:center;color:#7b8794">Nothing sold '+(which==='today'?'today':(prev?'in these dates last month':'yesterday'))+(bevBrand?(' for "'+esc(bevBrand)+'"'):'')+'.</td></tr>';
  }else{
    var html='';
    for(var k=0;k<items.length;k++){var it=items[k];
      // Realise = invoiced value / boxes, the same formula the Total Boxes card uses.
      // Guard on BOXES, not on the rate: an item given away free has 0 value and a real
      // realise of 0.00. A dash is reserved for "no boxes, so no rate exists".
      var rz=(it.boxes?((it.value||0)/it.boxes):null);
      html+='<tr><td>'+esc(it.item||'—')+'</td><td>'+esc(it.brand||'—')+'</td><td>'+esc(it.sku||'—')+'</td><td class="num">'+fN(Math.round((it.quantity||0)*100)/100)+'</td><td class="num">'+fN(Math.round((it.boxes||0)*100)/100)+'</td>'
           +'<td class="num" title="'+esc(fNp(it.value||0,2)+' over '+fN(Math.round((it.boxes||0)*100)/100)+' boxes')+'">'
           +(rz===null?'—':('₹'+fNp(rz,2)))+'</td></tr>';
    }
    /* The total realise is WEIGHTED - all the value over all the boxes - not the mean of the
       rates above, which would let a three-box line count as much as a 13,000-box one. */
    var avg=tb?(tv/tb):0;
    html+='<tr class="cd-total"><td colspan="3">TOTAL</td><td class="num">'+fN(Math.round(tq*100)/100)+'</td><td class="num">'+fN(Math.round(tb*100)/100)+'</td>'
         +'<td class="num" title="'+esc(fNp(tv,2)+' over '+fN(Math.round(tb*100)/100)+' boxes — weighted, not the mean of the rates above')+'">'
         +(tb?('₹'+fNp(avg,2)):'—')+'</td></tr>';
    body.innerHTML=html;
  }
  document.getElementById('bevSoldModal').classList.add('show');
}
function closeBevSold(){document.getElementById('bevSoldModal').classList.remove('show');}
var BEV_MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function bevFmtDate(iso){
  if(!iso)return '';
  var p=String(iso).split('-'); if(p.length!==3)return iso;
  return parseInt(p[2],10)+' '+(BEV_MON[parseInt(p[1],10)-1]||p[1])+' '+p[0];
}
// "1–11 Aug 2026" when both ends share a month, "15 Jul – 14 Aug 2026" when they do not.
function bevFmtRange(a,b){
  if(!a)return '';
  if(!b||b===a)return bevFmtDate(a);
  var p=String(a).split('-'), q=String(b).split('-');
  if(p.length!==3||q.length!==3)return bevFmtDate(a)+' – '+bevFmtDate(b);
  var mA=BEV_MON[parseInt(p[1],10)-1]||p[1], mB=BEV_MON[parseInt(q[1],10)-1]||q[1];
  if(p[0]===q[0]&&p[1]===q[1])return parseInt(p[2],10)+'–'+parseInt(q[2],10)+' '+mB+' '+q[0];
  if(p[0]===q[0])return parseInt(p[2],10)+' '+mA+' – '+parseInt(q[2],10)+' '+mB+' '+q[0];
  return bevFmtDate(a)+' – '+bevFmtDate(b);
}
// Sum quantity & boxes of a (brand-filtered) day item list.
function bevDayTotals(items,brand){var q=0,b=0,a=items||[];for(var i=0;i<a.length;i++){if(!brand||a[i].brand===brand){q+=a[i].quantity||0;b+=a[i].boxes||0;}}return {qty:Math.round(q*100)/100,boxes:Math.round(b*100)/100};}
function bevCmpChange(y,t){
  var d=Math.round((t-y)*100)/100;
  var cls=d>0?'pos':(d<0?'neg':''), arrow=d>0?'▲':(d<0?'▼':'–');
  var pct=y>0?((d/y)*100):(t>0?null:0);
  var pctStr=pct===null?'new':(d>=0?'+':'')+(Math.round(pct*10)/10)+'%';
  return '<span class="cmp-chg '+cls+'">'+arrow+' '+fN(Math.abs(d))+(y>0||pct===null?(' ('+pctStr+')'):'')+'</span>';
}
function openBevCompare(){
  if(!bevFetched){showToast('Fetch beverages first','info');return;}
  var tItems=(bevTodayItems||[]), yItems=(bevYestItems||[]);
  if(bevBrand){var bf=function(it){return it.brand===bevBrand;};tItems=tItems.filter(bf);yItems=yItems.filter(bf);}
  var tt=bevDayTotals(tItems,''), yt=bevDayTotals(yItems,'');
  document.getElementById('bevCmpSub').textContent=bevFmtDate(bevTodayDate)+' vs '+bevFmtDate(bevYestDate)+(bevBrand?(' · '+bevBrand):'');
  document.getElementById('bevCmpTHead').textContent='Today ('+bevFmtDate(bevTodayDate)+')';
  document.getElementById('bevCmpYHead').textContent='Yesterday ('+bevFmtDate(bevYestDate)+')';
  // Headline stat cards.
  var dBox=Math.round((tt.boxes-yt.boxes)*100)/100;
  var dCls=dBox>0?'pos':(dBox<0?'neg':''), dArrow=dBox>0?'▲':(dBox<0?'▼':'–');
  var dPct=yt.boxes>0?((dBox/yt.boxes)*100):(tt.boxes>0?null:0);
  var dUnit=dPct===null?'new sales':(yt.boxes>0?((dBox>=0?'+':'')+(Math.round(dPct*10)/10)+'% boxes'):'no change');
  document.getElementById('bevCmpStats').innerHTML=
    '<div class="bev-cmp-card"><div class="cmp-day">Today</div><div class="cmp-date">'+bevFmtDate(bevTodayDate)+'</div><div class="cmp-big">'+fN(tt.boxes)+'</div><div class="cmp-unit">boxes · '+fN(tt.qty)+' qty</div></div>'+
    '<div class="bev-cmp-card"><div class="cmp-day">Yesterday</div><div class="cmp-date">'+bevFmtDate(bevYestDate)+'</div><div class="cmp-big">'+fN(yt.boxes)+'</div><div class="cmp-unit">boxes · '+fN(yt.qty)+' qty</div></div>'+
    '<div class="bev-cmp-card"><div class="cmp-day">Change</div><div class="cmp-date">Boxes</div><div class="cmp-big '+dCls+'">'+dArrow+' '+fN(Math.abs(dBox))+'</div><div class="cmp-unit">'+dUnit+'</div></div>';
  // Per-item comparison, merged across both days.
  var map={}, order=[];
  function ensure(it){var k=(it.item||'—')+'|'+(it.brand||'—'); if(!map[k]){map[k]={item:it.item||'—',brand:it.brand||'—',y:0,t:0};order.push(k);} return map[k];}
  yItems.forEach(function(it){ensure(it).y+=it.boxes||0;});
  tItems.forEach(function(it){ensure(it).t+=it.boxes||0;});
  var list=order.map(function(k){return map[k];});
  list.sort(function(a,b){return Math.max(b.t,b.y)-Math.max(a.t,a.y);});
  var body=document.getElementById('bevCmpBody');
  if(!list.length){
    body.innerHTML='<tr><td colspan="5" style="padding:34px;text-align:center;color:#7b8794">No sales on either day'+(bevBrand?(' for "'+esc(bevBrand)+'"'):'')+'.</td></tr>';
  }else{
    var html='';
    for(var i=0;i<list.length;i++){var r=list[i];
      html+='<tr><td>'+esc(r.item)+'</td><td>'+esc(r.brand)+'</td><td class="num">'+fN(Math.round(r.y*100)/100)+'</td><td class="num">'+fN(Math.round(r.t*100)/100)+'</td><td class="num">'+bevCmpChange(r.y,r.t)+'</td></tr>';
    }
    html+='<tr class="cd-total"><td colspan="2">TOTAL</td><td class="num">'+fN(yt.boxes)+'</td><td class="num">'+fN(tt.boxes)+'</td><td class="num">'+bevCmpChange(yt.boxes,tt.boxes)+'</td></tr>';
    body.innerHTML=html;
  }
  document.getElementById('bevCompareModal').classList.add('show');
}
function closeBevCompare(){document.getElementById('bevCompareModal').classList.remove('show');}
// Aggregate customer_rows / month_rows by a key (honouring brand + month filters), boxes desc.
function bevAggTop(rows,keyField,brand,month){
  var map={}, order=[], a=rows||[];
  for(var i=0;i<a.length;i++){var r=a[i];
    if(brand&&r.brand!==brand)continue;
    if(month&&r.ym!==month)continue;
    var k=r[keyField]; if(k==null||k==='')continue;
    if(!map[k]){map[k]={key:k,label:r.label||k,boxes:0,quantity:0};order.push(k);}
    map[k].boxes+=r.boxes||0; map[k].quantity+=r.quantity||0;
  }
  var list=order.map(function(k){return map[k];});
  list.sort(function(a,b){return (b.boxes-a.boxes)||String(a.label).localeCompare(String(b.label));});
  return list;
}
function openBevTop(which){
  if(!bevFetched){showToast('Fetch beverages first','info');return;}
  var isCust=(which==='customers'), list=isCust?bevTopCustList:bevTopMonthList;
  document.getElementById('bevTopTitle').textContent=(isCust?'Top Customers':'Top Months')+(bevBrand?(' — '+bevBrand):'');
  document.getElementById('bevTopNameHead').textContent=isCust?'Customer':'Month';
  var tb=0,tq=0; for(var i=0;i<list.length;i++){tb+=list[i].boxes;tq+=list[i].quantity;}
  document.getElementById('bevTopSub').textContent=list.length+(isCust?' customer(s) · ':' month(s) · ')+fN(Math.round(tb*100)/100)+' boxes — highest first';
  var body=document.getElementById('bevTopBody');
  if(!list.length){
    body.innerHTML='<tr><td colspan="4" style="padding:34px;text-align:center;color:#7b8794">No data'+(bevBrand?(' for "'+esc(bevBrand)+'"'):'')+'.</td></tr>';
  }else{
    var html='';
    for(var k=0;k<list.length;k++){var r=list[k];
      html+='<tr><td>'+(k+1)+'</td><td>'+esc(r.label)+'</td><td class="num">'+fN(Math.round(r.boxes*100)/100)+'</td><td class="num">'+fN(Math.round(r.quantity*100)/100)+'</td></tr>';
    }
    html+='<tr class="cd-total"><td colspan="2">TOTAL</td><td class="num">'+fN(Math.round(tb*100)/100)+'</td><td class="num">'+fN(Math.round(tq*100)/100)+'</td></tr>';
    body.innerHTML=html;
  }
  document.getElementById('bevTopModal').classList.add('show');
}
function closeBevTop(){document.getElementById('bevTopModal').classList.remove('show');}
// ── Customer Grading popup — customers (rows) × months (cols), avg sales (boxes) + grade ──
// Grade is relative to the top customer's average so it self-scales to any volume:
// A ≥ 75% of the highest avg, B ≥ 50%, C ≥ 25%, D below. Honours the Brand filter.
function bevGradeFor(avg,maxAvg){
  if(maxAvg<=0)return 'D';
  var r=avg/maxAvg;
  return r>=0.75?'A':r>=0.5?'B':r>=0.25?'C':'D';
}
function openBevGrading(){
  if(!bevFetched){showToast('Fetch beverages first','info');return;}
  // Month columns: every ym present, oldest → newest, labelled from month_rows.
  var labels={}; for(var i=0;i<bevMonthRows.length;i++){var m=bevMonthRows[i]; if(m.ym)labels[m.ym]=m.label||m.ym;}
  var monthSet={};
  // customer → {ym → boxes}, honouring the brand filter.
  var cust={}, order=[];
  for(var j=0;j<bevCustomerRows.length;j++){
    var r=bevCustomerRows[j];
    if(bevBrand&&r.brand!==bevBrand)continue;
    var name=r.customer||'—', ym=r.ym||'';
    if(!ym)continue;
    monthSet[ym]=1;
    if(!cust[name]){cust[name]={name:name,by:{},total:0};order.push(name);}
    cust[name].by[ym]=(cust[name].by[ym]||0)+(r.boxes||0);
    cust[name].total+=r.boxes||0;
  }
  var months=Object.keys(monthSet).sort();   // YYYY-MM sorts chronologically
  var nMonths=months.length||1;
  var list=order.map(function(n){var c=cust[n]; c.avg=c.total/nMonths; return c;});
  var maxAvg=0; for(var a=0;a<list.length;a++)if(list[a].avg>maxAvg)maxAvg=list[a].avg;
  list.forEach(function(c){c.grade=bevGradeFor(c.avg,maxAvg);});
  list.sort(function(x,y){return y.avg-x.avg||String(x.name).localeCompare(String(y.name));});

  document.getElementById('bevGradeSub').textContent=
    list.length+' customer(s) · '+months.length+' month(s) · avg sales in boxes'+(bevBrand?(' · '+bevBrand):'')+' · grade vs. top customer';
  document.getElementById('bevGradeHead').innerHTML='<tr><th class="gc-name">Customer</th>'
    +months.map(function(m){return '<th class="num">'+esc(labels[m]||m)+'</th>';}).join('')
    +'<th class="num gc-avg">Avg Sales</th><th class="num">Grade</th></tr>';
  var body=document.getElementById('bevGradeBody');
  if(!list.length){
    body.innerHTML='<tr><td colspan="'+(months.length+3)+'" style="padding:34px;text-align:center;color:#7b8794">No customer sales'+(bevBrand?(' for "'+esc(bevBrand)+'"'):'')+'.</td></tr>';
  }else{
    var html='', colTot={}, gtot=0;
    for(var k=0;k<list.length;k++){var c=list[k];
      var cells=months.map(function(m){var v=c.by[m]||0;colTot[m]=(colTot[m]||0)+v;return '<td class="num">'+(v?fN(Math.round(v*100)/100):'')+'</td>';}).join('');
      gtot+=c.total;
      html+='<tr><td class="gc-name">'+esc(c.name)+'</td>'+cells
        +'<td class="num gc-avg">'+fN(Math.round(c.avg*100)/100)+'</td>'
        +'<td class="num"><span class="bev-grade-badge g'+c.grade+'">'+c.grade+'</span></td></tr>';
    }
    var totCells=months.map(function(m){return '<td class="num">'+fN(Math.round((colTot[m]||0)*100)/100)+'</td>';}).join('');
    html+='<tr class="cd-total"><td class="gc-name">TOTAL</td>'+totCells
      +'<td class="num gc-avg">'+fN(Math.round((gtot/nMonths)*100)/100)+'</td><td class="num">—</td></tr>';
    body.innerHTML=html;
  }
  bevGradeData={list:list, months:months, labels:labels, brand:bevBrand, nMonths:nMonths};
  document.getElementById('bevGradeModal').classList.add('show');
}
function closeBevGrading(){document.getElementById('bevGradeModal').classList.remove('show');}

// Export the Customer Grading table (customers × months + Avg Sales + Grade) to Excel.
var bevGradeData=null;
function exportBevGrading(){
  var d=bevGradeData;
  if(!d||!d.list.length){showToast('Nothing to export — open grading first','info');return;}
  function r2(v){return Math.round((Number(v)||0)*100)/100;}
  var hd=['Customer'].concat(d.months.map(function(m){return d.labels[m]||m;})).concat(['Avg Sales','Grade']);
  var out=[hd.map(function(h){return {value:h,bold:true,fill:'0F172A',color:'FFFFFF'};})];
  var colTot={}, gtot=0;
  d.list.forEach(function(c){
    var cells=d.months.map(function(m){var v=c.by[m]||0; colTot[m]=(colTot[m]||0)+v; return v?r2(v):'';});
    gtot+=c.total;
    out.push([c.name].concat(cells).concat([r2(c.avg), c.grade]));
  });
  var totCells=d.months.map(function(m){return {value:r2(colTot[m]||0),bold:true};});
  out.push([{value:'TOTAL',bold:true}].concat(totCells).concat([{value:r2(gtot/d.nMonths),bold:true},'']));
  var fn='Beverages_Customer_Grading'+(d.brand?('_'+String(d.brand).replace(/[^A-Za-z0-9]+/g,'_')):'');
  fetch('/realise/api/export-xlsx/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},
    body:JSON.stringify({filename:fn,sheets:[{name:'Customer Grading',rows:out}]})})
    .then(function(r){return r.ok?r.blob():null;})
    .then(function(b){ if(!b){showToast('Export failed','error');return;} var u=URL.createObjectURL(b),el=document.createElement('a');el.href=u;el.download=fn+'.xlsx';document.body.appendChild(el);el.click();el.remove();URL.revokeObjectURL(u); });
}
// ── Order in Hand popup — dynamic drill over open-order rows ────────────────
function bevOihActive(){
  return (bevOihRows||[]).filter(function(r){return (!bevBrand||r.brand===bevBrand)&&(!bevMonth||r.ym===bevMonth);});
}
function renderOihChips(){
  var box=document.getElementById('oihChips'); if(!box)return;
  var html='';
  OIH_DIMS.forEach(function(d){
    var pos=oihPopOrder.indexOf(d.key), on=pos!==-1;
    html+='<button type="button" class="oih-chip'+(on?' active':'')+'" data-oihdim="'+d.key+'">'+esc(d.label)+(on?'<span class="oih-pos">'+(pos+1)+'</span>':'')+'</button>';
  });
  box.innerHTML=html;
}
function bevOihTreeRows(nodes,level){
  var html='';
  for(var i=0;i<nodes.length;i++){var n=nodes[i];
    var hasKids=n.kids&&n.kids.length>0, open=!!oihPopExpanded[n.path];
    var tw=hasKids?'<span class="bev-tw'+(open?' open':'')+'" data-oihpath="'+esc(n.path)+'">&#9654;</span>':'<span class="bev-tw-empty"></span>';
    var tag='<span class="cd-tag '+(BEV_TAG_CLS[n.dim]||'')+'">'+esc(BEV_DIM_NAME[n.dim]||n.dim)+'</span>';
    html+='<tr><td style="padding-left:'+(12+level*18)+'px">'+tw+tag+esc(n.name)+'</td><td class="num">'+fN(n.quantity)+'</td><td class="num">'+fN(n.boxes)+'</td></tr>';
    if(hasKids&&open)html+=bevOihTreeRows(n.kids,level+1);
  }
  return html;
}
function oihPopRender(){
  renderOihChips();
  var label=oihPopOrder.map(function(k){return BEV_DIM_NAME[k];}).join(' › ')||'Dimension';
  document.getElementById('oihFirstCol').textContent=label;
  var rows=bevOihActive();
  var tp=0,tb=0; for(var i=0;i<rows.length;i++){tp+=rows[i].quantity||0;tb+=rows[i].boxes||0;}
  tp=Math.round(tp*100)/100; tb=Math.round(tb*100)/100;
  document.getElementById('bevOihSub').textContent=fN(tb)+' boxes · '+fN(tp)+' pcs open'+(bevBrand?(' · '+bevBrand):'')+(bevMonth?(' · '+bevMonth):'');
  var body=document.getElementById('oihBody');
  if(!oihPopOrder.length){body.innerHTML='<tr><td colspan="3" style="padding:34px;text-align:center;color:#7b8794">Pick at least one drill dimension.</td></tr>';return;}
  if(!rows.length){body.innerHTML='<tr><td colspan="3" style="padding:34px;text-align:center;color:#7b8794">No open orders'+(bevBrand?(' for "'+esc(bevBrand)+'"'):'')+'.</td></tr>';return;}
  var tree=buildBevTree(rows,oihPopOrder,0,'');
  var html=bevOihTreeRows(tree,0);
  html+='<tr class="cd-total"><td>TOTAL</td><td class="num">'+fN(tp)+'</td><td class="num">'+fN(tb)+'</td></tr>';
  body.innerHTML=html;
}
function openBevOih(){
  if(!bevFetched){showToast('Fetch beverages first','info');return;}
  oihPopExpanded={};
  oihPopRender();
  document.getElementById('bevOihModal').classList.add('show');
}
function closeBevOih(){document.getElementById('bevOihModal').classList.remove('show');}
// Drill-chip toggle (click order sets the drill order) + row expand, scoped to the OIH popup.
document.addEventListener('click',function(e){
  var chip=e.target&&e.target.closest?e.target.closest('#oihChips .oih-chip'):null;
  if(chip){var k=chip.getAttribute('data-oihdim'),i=oihPopOrder.indexOf(k);
    if(i===-1)oihPopOrder.push(k); else oihPopOrder.splice(i,1);
    oihPopExpanded={}; oihPopRender(); return;}
  var tw=e.target&&e.target.closest?e.target.closest('#oihBody .bev-tw'):null;
  if(tw){var p=tw.getAttribute('data-oihpath'); oihPopExpanded[p]=!oihPopExpanded[p]; oihPopRender();}
});
function bevRenderDpList(){
  var list=document.getElementById('bevDpList'); if(!list)return; list.innerHTML='';
  BEV_DIMS.forEach(function(d){
    var pos=bevChosen.indexOf(d.key);
    var item=document.createElement('label');
    item.className='com-dp-item'+(pos!==-1?' checked':'');
    item.innerHTML='<input type="checkbox" '+(pos!==-1?'checked':'')+'><span class="com-dp-name">'+d.label+'</span><span class="com-pos">'+(pos!==-1?pos+1:'')+'</span>';
    item.querySelector('input').addEventListener('change',function(){
      var i=bevChosen.indexOf(d.key);
      if(this.checked){ if(i===-1)bevChosen.push(d.key); } else if(i!==-1)bevChosen.splice(i,1);
      bevRenderDpList();
    });
    list.appendChild(item);
  });
}
function bevInitDrillPanel(){
  var btn=document.getElementById('bevDrillBtn'), panel=document.getElementById('bevDrillPanel');
  if(!btn||!panel)return;
  btn.addEventListener('click',function(e){ e.stopPropagation(); bevChosen=bevOrder.slice(); bevRenderDpList(); panel.classList.toggle('open'); });
  panel.addEventListener('click',function(e){ e.stopPropagation(); });
  document.addEventListener('click',function(){ panel.classList.remove('open'); });
  document.getElementById('bevSelAll').addEventListener('click',function(){ bevChosen=BEV_DIMS.map(function(d){return d.key;}); bevRenderDpList(); });
  document.getElementById('bevClrAll').addEventListener('click',function(){ bevChosen=[]; bevRenderDpList(); });
  document.getElementById('bevApply').addEventListener('click',function(){
    if(!bevChosen.length){ showToast('Pick at least one dimension','info'); return; }
    bevOrder=bevChosen.slice(); bevExpanded={}; bevDocState={};
    panel.classList.remove('open'); renderBeverages();
  });
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',bevInitDrillPanel);
else bevInitDrillPanel();
document.addEventListener('click',function(e){
  if(!e.target||!e.target.closest)return;
  var v=e.target.closest('#bevBody .bev-docval');
  if(v){ bevToggleDocs(v.getAttribute('data-bevpath'), v.getAttribute('data-metric')); return; }
  var b=e.target.closest('#bevBody .bev-tw');
  if(!b)return;
  var p=b.getAttribute('data-bevpath'); bevExpanded[p]=!bevExpanded[p];
  renderBeverages();
});
function exportBeveragesCSV(){
  if(!bevFetched||!bevActiveRows().length){showToast('Fetch beverages first','info');return;}
  var arows=bevActiveRows();
  var out=[[bevOrder.map(function(k){return BEV_DIM_NAME[k];}).join(' > '),'Quantity','Boxes','OIH']];
  (function walk(nodes,prefix){
    nodes.forEach(function(n){
      out.push([prefix+n.name, Math.round(n.quantity*100)/100, Math.round(n.boxes*100)/100, Math.round((n.oih||0)*100)/100]);
      if(n.kids&&n.kids.length)walk(n.kids,prefix+'    ');
    });
  })(bevTree.length?bevTree:buildBevTree(arows,bevOrder,0,''),'');
  var tq=0,tb=0,toih=0; for(var i=0;i<arows.length;i++){tq+=arows[i].quantity||0;tb+=arows[i].boxes||0;toih+=arows[i].oih||0;}
  out.push(['TOTAL',Math.round(tq*100)/100,Math.round(tb*100)/100,Math.round(toih*100)/100]);
  var text=out.map(function(r){return r.map(function(c){c=String(c==null?'':c);return /[",\r\n]/.test(c)?('"'+c.replace(/"/g,'""')+'"'):c;}).join(',');}).join('\r\n');
  var blob=new Blob(['\uFEFF'+text],{type:'text/csv;charset=utf-8;'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download='Beverages_'+document.getElementById('bevFrom').value+'_'+document.getElementById('bevTo').value+'.csv';a.click();
  setTimeout(function(){URL.revokeObjectURL(url);},1000);
  showToast('CSV exported','ok');
}

function getDrillBy(){return document.getElementById('fDrill').value;}
function productKey(r){return r.u_type+'|'+r.u_sub_group;}
function getAvgRealise(r){return avgRealiseData[productKey(r)]||0;}
function getDrillAvgRealise(uType,uSub,drillCol,dimValue){
  var key=uType+'|'+uSub+'|'+drillCol+'|'+dimValue.toUpperCase();
  return avgRealiseDrill[key]||0;
}
function usedDimsInPath(path){var dims=[];var parts=path.split('>');for(var i=1;i<parts.length;i++)dims.push(parts[i].split('=')[0]);if(drillDim[path])dims.push(drillDim[path]);return dims;}
function filtersFromPath(path){var f={};var parts=path.split('>');for(var i=1;i<parts.length;i++){var eq=parts[i].indexOf('=');f[parts[i].substring(0,eq)]=parts[i].substring(eq+1);}return f;}
function clearChildren(pp){var pfx=pp+'>';var ks=Object.keys(drillData);for(var i=0;i<ks.length;i++){if(ks[i].indexOf(pfx)===0){delete drillData[ks[i]];delete drillOpen[ks[i]];delete drillDim[ks[i]];}}}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function onDrillChange(){if(!getDrillBy()){drillData={};drillOpen={};drillDim={};rootDrillDim='';}renderTable();}

var allExpanded=false;
function toggleExpandAll(){if(allExpanded){collapseAll();}else{expandAll();}}

async function expandAll(){
  var db=getDrillBy();if(!db){showToast('Select a Drill By option first','info');return;}
  if(!dataFetched){showToast('Fetch data first','info');return;}
  var btn=document.getElementById('expandAllBtn');btn.disabled=true;btn.textContent='Expanding...';
  var useDb=rootDrillDim||db;
  if(!rootDrillDim)rootDrillDim=useDb;
  var count=0;
  for(var i=0;i<filtered.length;i++){
    var row=filtered[i],pk=productKey(row);
    if(row._nodata||row.litres<=0)continue;
    if(drillOpen[pk]&&drillDim[pk]===useDb)continue;
    clearChildren(pk);delete drillData[pk];delete drillDim[pk];
    await fetchAndExpand(pk,row.u_type,row.u_sub_group,pk,useDb);
    count++;
  }
  allExpanded=true;btn.disabled=false;btn.innerHTML='&#9650; Collapse All';
  showToast('Expanded '+count+' products','ok');
}

function collapseAll(){
  for(var i=0;i<filtered.length;i++){var pk=productKey(filtered[i]);drillOpen[pk]=false;}
  allExpanded=false;document.getElementById('expandAllBtn').innerHTML='&#9660; Expand All';
  renderTable();showToast('All collapsed','ok');
}

async function onAvgPeriodChange(){
  avgPeriod=document.getElementById('fAvgPeriod').value;
  if(!avgPeriod){avgRealiseData={};avgRealiseDrill={};renderTable();return;}
  if(!dataFetched){showToast('Fetch data first','info');document.getElementById('fAvgPeriod').value='';return;}
  var sd=document.getElementById('dFrom').value,ed=document.getElementById('dTo').value;
  try{
    var res=await fetch(API+'/api/historical-realise/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify({start_date:sd,end_date:ed,period:avgPeriod})});
    if(!res.ok)throw new Error('Failed');
    var result=await res.json();
    avgRealiseData=result.data||{};avgRealiseDrill=result.drill_data||{};
    renderTable();updateSummary();
    var labels={'12m':'12 Month','6m':'6 Month','3m':'Quarterly','last_month':'Last Month'};
    showToast(labels[avgPeriod]+' avg realise loaded','ok');
  }catch(e){showToast('Avg realise error: '+e.message,'err');}
}

function wasPageReload(){
  try{
    var nav=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];
    return nav&&nav.type==='reload';
  }catch(e){return false;}
}
function clearCachedDashboardOnReload(){
  var nocache=new URLSearchParams(window.location.search).get('nocache')==='1';
  if(wasPageReload()||nocache){
    try{sessionStorage.removeItem(CACHE_KEY);}catch(e){}
    try{sessionStorage.removeItem(SC2_CACHE_KEY);}catch(e){}
    try{sessionStorage.removeItem('cs_compare_v1');}catch(e){}   // Compare Sales pivot cache
  }
}
function saveDashboardCache(){
  try{
    sessionStorage.setItem(CACHE_KEY,JSON.stringify({
      start_date:document.getElementById('dFrom').value,
      end_date:document.getElementById('dTo').value,
      data:allData,
      channel_rows:channelRows,
      channel_month_rows:sc2MonthRows,
      saved_at:Date.now()
    }));
  }catch(e){console.warn('[REALISE CACHE] Could not save dashboard cache:',e);}
}
async function restoreDashboardCache(){
  try{
    var raw=sessionStorage.getItem(CACHE_KEY);
    if(!raw)return false;
    var cached=JSON.parse(raw);
    if(!cached||!Array.isArray(cached.data))return false;
    if(cached.start_date)document.getElementById('dFrom').value=cached.start_date;
    if(cached.end_date)document.getElementById('dTo').value=cached.end_date;
    if(Array.isArray(cached.channel_month_rows))sc2MonthRows=cached.channel_month_rows;
    await hydrateFetchedData(cached.data,cached.channel_rows||[],{toast:false});
    return true;
  }catch(e){
    console.warn('[REALISE CACHE] Could not restore dashboard cache:',e);
    try{sessionStorage.removeItem(CACHE_KEY);}catch(_e){}
    return false;
  }
}

// Slide 2 has its own date range/segment/data, so it caches separately. Like slide 1,
// the cache survives in-tab navigation but is wiped on a hard refresh (see
// clearCachedDashboardOnReload), so opening the dashboard reuses the data and only
// a real refresh (or the Fetch button) re-hits SAP.
function saveSlideTwoCache(){
  try{
    var segEl=document.getElementById('sc2Segment');
    sessionStorage.setItem(SC2_CACHE_KEY,JSON.stringify({
      start_date:document.getElementById('sc2From').value,
      end_date:document.getElementById('sc2To').value,
      segment:segEl?segEl.value:'',
      channel_rows:sc2Rows,
      channel_month_rows:sc2MonthRows,
      saved_at:Date.now()
    }));
  }catch(e){console.warn('[REALISE CACHE] Could not save slide-2 cache:',e);}
}
function restoreSlideTwoCache(){
  try{
    var raw=sessionStorage.getItem(SC2_CACHE_KEY);
    if(!raw)return false;
    var cached=JSON.parse(raw);
    if(!cached||!Array.isArray(cached.channel_rows))return false;
    if(cached.start_date)document.getElementById('sc2From').value=cached.start_date;
    if(cached.end_date)document.getElementById('sc2To').value=cached.end_date;
    var segEl=document.getElementById('sc2Segment');
    if(segEl&&typeof cached.segment==='string')segEl.value=cached.segment;
    sc2Rows=cached.channel_rows;
    if(Array.isArray(cached.channel_month_rows))sc2MonthRows=cached.channel_month_rows;
    sc2Fetched=true;
    sc2Rendered=false;  // grid not painted yet; setSlide will render from cache
    return true;
  }catch(e){
    console.warn('[REALISE CACHE] Could not restore slide-2 cache:',e);
    try{sessionStorage.removeItem(SC2_CACHE_KEY);}catch(_e){}
    return false;
  }
}

document.addEventListener('DOMContentLoaded',function(){
  clearCachedDashboardOnReload();
  var n=new Date();
  document.getElementById('dFrom').value=n.getFullYear()+'-'+String(n.getMonth()+1).padStart(2,'0')+'-01';
  document.getElementById('dTo').value=n.toISOString().split('T')[0];
  checkHealth();
  // Channel/beverages dashboard is the default first slide now (internal index 1);
  // #products deep-links to the OILS product table (internal index 0, shown second).
  var startSlide=(location.hash==='#products')?0:1;
  restoreSlideTwoCache();
  setSlide(startSlide);
  applyRoleRestrictions();
  loadSavedTargets().then(function(){
    restoreDashboardCache().then(function(restored){
      if(!restored){renderInitialTable();loadData({auto:true});}
    });
  });
});

async function checkHealth(){
  try{
    var r=await fetch(API+'/api/health/',{headers:{'X-CSRFToken':getCSRF()}});
    var d=await r.json();
    document.getElementById('sDot').className=d.sap_connected?'dot dot-green':'dot dot-red';
    document.getElementById('sTxt').textContent=d.sap_connected?'SAP Connected':'SAP Error';
  }catch(e){
    document.getElementById('sDot').className='dot dot-red';
    document.getElementById('sTxt').textContent='Server Offline';
  }
}

function realKey(r){return r.u_type+'|'+r.u_sub_group+'|'+r.month+'|'+r.year;}
function isAggregatedView(){return document.getElementById('fMonth').value==='';}
function findUnderlyingRows(t,s){var ft=document.getElementById('fType').value,fy=document.getElementById('fYear').value,o=[];for(var i=0;i<allData.length;i++){var r=allData[i];if(r.u_type!==t||r.u_sub_group!==s)continue;if(ft&&r.u_type!==ft)continue;if(fy&&r.year!==fy)continue;o.push(r);}return o;}
function setDirty(v){isDirty=v;}

function populateFilters(months,years){
  var ms=document.getElementById('fMonth');ms.innerHTML='<option value="">All Months</option>';
  var ys=document.getElementById('fYear');ys.innerHTML='<option value="">All Years</option>';
  for(var i=0;i<months.length;i++){var o=document.createElement('option');o.value=months[i];o.textContent=months[i];ms.appendChild(o);}
  for(var j=0;j<years.length;j++){var o=document.createElement('option');o.value=years[j];o.textContent=years[j];ys.appendChild(o);}
}

async function renderInitialTable(){
  filtered=[];
  var roleType=currentUser&&currentUser.type_filter?currentUser.type_filter:'';
  for(var i=0;i<PRODUCTS.length;i++){
    var p=PRODUCTS[i];
    if(!isProdSelected(p.u_type,p.u_sub_group))continue;
    if(roleType&&p.u_type!==roleType)continue;
    var ts=getDefTS(p.u_type,p.u_sub_group);
    var tr=getDefTR(p.u_type,p.u_sub_group);
    filtered.push({u_type:p.u_type,u_sub_group:p.u_sub_group,month:'',year:'',litres:0,linetotal:0,realise:0,target_sale:ts,target_realise:tr,_nodata:true});
  }
  filtered.sort(function(a,b){return (b.u_type.localeCompare(a.u_type))||((a.target_sale||0)<(b.target_sale||0)?1:(a.target_sale||0)>(b.target_sale||0)?-1:0);});
  renderTable();updateSummary();if(currentSlide===1)await renderSlideTwo();
}

async function hydrateFetchedData(rows,rawChannelRows,opts){
  opts=opts||{};
  await loadSavedTargets();
  allData=(rows||[]).filter(function(r){return r.u_type==='COMMODITY'||r.u_type==='PREMIUM';});
  channelRows=Array.isArray(rawChannelRows)?rawChannelRows:[];
  // Slide 2 shares the same channel rows, so a restored slide-1 cache lights it up too.
  sc2Rows=channelRows; sc2Fetched=true; sc2Rendered=false;
  commodityLastRows=null;  // last-month done depends on the date range; refetch lazily
  dataFetched=true;modifiedRealKeys.clear();setDirty(false);
  for(var i=0;i<allData.length;i++){
    var dr=allData[i];
    if(dr.target_sale==null||dr.target_sale===undefined)dr.target_sale=getDefTS(dr.u_type,dr.u_sub_group);
    if(dr.target_realise==null||dr.target_realise===undefined)dr.target_realise=getDefTR(dr.u_type,dr.u_sub_group);
  }
  var mSet={},ySet={};
  for(var mi=0;mi<allData.length;mi++){if(allData[mi].month)mSet[allData[mi].month]=1;if(allData[mi].year)ySet[allData[mi].year]=1;}
  var mArr=Object.keys(mSet).sort(function(a,b){return MONTHS.indexOf(a)-MONTHS.indexOf(b);});
  var yArr=Object.keys(ySet).sort();
  populateFilters(mArr,yArr);
  var cm=currentMonth(),ms=document.getElementById('fMonth'),found=false;
  for(var j=0;j<ms.options.length;j++){if(ms.options[j].value===cm){found=true;break;}}
  if(found)ms.value=cm;else ms.value='';
  document.getElementById('fType').value=currentUser&&currentUser.type_filter?currentUser.type_filter:'';
  document.getElementById('fYear').value='';
  await applyFilters();
  if(opts.toast!==false)showToast(allData.length+' rows loaded - showing '+(found?cm:'All Months'),'ok');
}

// Both slides share ONE fetch. Either Fetch button (or the date pickers on either
// slide) drives this; whichever slide triggered it, the chosen date range is mirrored
// to the other slide and the single SAP response hydrates both. So fetching from
// slide 2 also refreshes slide 1, and vice versa — no need to visit slide 1 first.
async function loadData(opts){
  opts=opts||{};
  var src=opts.source==='slide2'?'slide2':'slide1';
  var fromEl=src==='slide2'?'sc2From':'dFrom', toEl=src==='slide2'?'sc2To':'dTo';
  var silent=!!opts.silent;   // background auto-refresh: no overlay/spinner/toast, keep filters
  var sd=document.getElementById(fromEl).value, ed=document.getElementById(toEl).value;
  if(!sd||!ed){ if(!silent)showToast('Select dates','err'); return; }
  if(sc2Loading)return; sc2Loading=true;
  // Keep both slides' date pickers in lockstep.
  document.getElementById('dFrom').value=sd;document.getElementById('dTo').value=ed;
  document.getElementById('sc2From').value=sd;document.getElementById('sc2To').value=ed;
  var fb=document.getElementById('fetchBtn'); if(fb&&!silent){fb.disabled=true;fb.textContent='Loading...';}
  var sb=document.getElementById('sc2FetchBtn'); if(sb&&!silent){sb.disabled=true;sb.innerHTML='Loading…';}
  if(!silent)document.getElementById('ov').classList.add('show');
  var grid=document.getElementById('channelGrid');
  if(!silent&&grid&&currentSlide===1&&!sc2Rendered)grid.innerHTML='<div class="sc2-empty" style="grid-column:1 / -1">Loading channel data…</div>';
  if(!silent){drillData={};drillOpen={};drillDim={};rootDrillDim='';allExpanded=false;
    document.getElementById('expandAllBtn').innerHTML='&#9660; Expand All';}
  try{
    // Start the big sales pull FIRST, then load targets while it is in flight.
    // Targets are only needed once we render, so waiting for them before even
    // asking for the sales data just added the two waits together.
    var salesReq=fetch(API+'/api/sales-data/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify({start_date:sd,end_date:ed,refresh:!!opts.force})});
    await loadSavedTargets();
    var res=await salesReq;
    if(!res.ok){var e=await res.json();throw new Error(e.detail||e.error||'Server error');}
    var result=await res.json();
    allData=(result.data||[]).filter(function(r){return r.u_type==='COMMODITY'||r.u_type==='PREMIUM';});
    channelRows=Array.isArray(result.channel_rows)?result.channel_rows:[];
    sc2MonthRows=Array.isArray(result.channel_month_rows)?result.channel_month_rows:[];
    // Slide 2 reads the very same payload.
    sc2Rows=channelRows; sc2Fetched=true; sc2Rendered=false;
    commodityLastRows=null;  // last-month done depends on the date range; refetch lazily
    dataFetched=true;modifiedRealKeys.clear();setDirty(false);
    for(var i=0;i<allData.length;i++){
      var dr=allData[i];
      if(dr.target_sale==null||dr.target_sale===undefined)dr.target_sale=getDefTS(dr.u_type,dr.u_sub_group);
      if(dr.target_realise==null||dr.target_realise===undefined)dr.target_realise=getDefTR(dr.u_type,dr.u_sub_group);
    }
    var mSet={},ySet={};
    for(var mi=0;mi<allData.length;mi++){if(allData[mi].month)mSet[allData[mi].month]=1;if(allData[mi].year)ySet[allData[mi].year]=1;}
    var mArr=Object.keys(mSet).sort(function(a,b){return MONTHS.indexOf(a)-MONTHS.indexOf(b);});
    var yArr=Object.keys(ySet).sort();
    var pType=document.getElementById('fType').value, pMonth=document.getElementById('fMonth').value, pYear=document.getElementById('fYear').value;
    populateFilters(mArr,yArr);
    var cm=currentMonth(),ms=document.getElementById('fMonth'),found=false;
    for(var j=0;j<ms.options.length;j++){if(ms.options[j].value===cm){found=true;break;}}
    if(silent){
      // Background refresh: keep the user's current filter selection rather than snapping back
      // to the default month/type. Restore each only if its option still exists after reload.
      var hasM=false;for(var jm=0;jm<ms.options.length;jm++){if(ms.options[jm].value===pMonth){hasM=true;break;}}
      ms.value=hasM?pMonth:'';
      document.getElementById('fType').value=pType;
      var ys=document.getElementById('fYear'),hasY=false;for(var jy=0;jy<ys.options.length;jy++){if(ys.options[jy].value===pYear){hasY=true;break;}}
      document.getElementById('fYear').value=hasY?pYear:'';
    }else{
      if(found)ms.value=cm;else ms.value='';
      document.getElementById('fType').value=currentUser&&currentUser.type_filter?currentUser.type_filter:'';document.getElementById('fYear').value='';
    }
    await applyFilters();   // renders slide 1 table + slide 2 cards (via renderSlideTwo)
    saveDashboardCache();
    saveSlideTwoCache();
    arMarkUpdated();        // stamp the freshness indicator (manual or silent auto-refresh)
    if(opts.toast!==false){ if(silent)showToast('Auto-refreshed · '+allData.length+' rows','info');
      else showToast(allData.length+' rows loaded — showing '+(found?cm:'All Months'),'ok'); }
  }catch(e){ if(!silent){ showToast('Error: '+e.message,'err'); if(grid&&currentSlide===1)grid.innerHTML='<div class="sc2-empty" style="grid-column:1 / -1">Error: '+esc(e.message)+'</div>'; } }
  finally{
    sc2Loading=false;
    if(fb&&!silent){fb.disabled=false;fb.innerHTML='&#9654; Fetch Data';}
    if(sb&&!silent){sb.disabled=false;sb.innerHTML='&#9654; Fetch';}
    if(!silent)document.getElementById('ov').classList.remove('show');
  }
}

async function applyFilters(){
  if(!dataFetched){renderInitialTable();return;}
  await loadSavedTargets();
  for(var _i=0;_i<allData.length;_i++){
    var _dr=allData[_i];var _k=_dr.u_type+'|'+_dr.u_sub_group;
    if(savedTargets[_k]){if(savedTargets[_k].tgt_ltrs!=null)_dr.target_sale=savedTargets[_k].tgt_ltrs;if(savedTargets[_k].tgt_rate!=null)_dr.target_realise=savedTargets[_k].tgt_rate;}
  }
  drillData={};drillOpen={};drillDim={};rootDrillDim='';allExpanded=false;
  document.getElementById('expandAllBtn').innerHTML='&#9660; Expand All';
  var ft=document.getElementById('fType').value,fm=document.getElementById('fMonth').value,fy=document.getElementById('fYear').value;
  var rows=[];for(var i=0;i<allData.length;i++){var r=allData[i];if(ft&&r.u_type!==ft)continue;if(fm&&r.month!==fm)continue;if(fy&&r.year!==fy)continue;if(!isProdSelected(r.u_type,r.u_sub_group))continue;rows.push(r);}
  if(!fm){
    var agg={};for(var i=0;i<rows.length;i++){var r=rows[i],gk=r.u_type+'|'+r.u_sub_group;
      if(!agg[gk])agg[gk]={u_type:r.u_type,u_sub_group:r.u_sub_group,month:'ALL',year:fy||'',litres:0,linetotal:0,target_sale:0,target_realise:getDefTR(r.u_type,r.u_sub_group),_count:0,_nodata:false};
      var a=agg[gk];a.litres+=r.litres;a.linetotal+=r.linetotal;a.target_sale+=(r.target_sale||0);a._count++;}
    var prods=ft?PRODUCTS.filter(function(p){return p.u_type===ft;}):PRODUCTS;
    prods=prods.filter(function(p){return isProdSelected(p.u_type,p.u_sub_group);});
    for(var pi=0;pi<prods.length;pi++){var pk=prods[pi].u_type+'|'+prods[pi].u_sub_group;
      if(!agg[pk]){var mc=0;var sm={};for(var mi=0;mi<allData.length;mi++){if(!sm[allData[mi].month]){sm[allData[mi].month]=true;mc++;}}
        agg[pk]={u_type:prods[pi].u_type,u_sub_group:prods[pi].u_sub_group,month:'ALL',year:fy||'',litres:0,linetotal:0,
          target_sale:getDefTS(prods[pi].u_type,prods[pi].u_sub_group)*mc,
          target_realise:getDefTR(prods[pi].u_type,prods[pi].u_sub_group),_count:0,_nodata:false};}}
    var vals=[],keys=Object.keys(agg);for(var k=0;k<keys.length;k++){var g=agg[keys[k]];g.realise=g.litres>0?Math.round((g.linetotal/g.litres)*100)/100:0;vals.push(g);}
    filtered=vals.sort(function(a,b){return (b.u_type.localeCompare(a.u_type))||((a.target_sale||0)<(b.target_sale||0)?1:(a.target_sale||0)>(b.target_sale||0)?-1:0);});
  }else{
    for(var ei=0;ei<rows.length;ei++)rows[ei]._nodata=false;
    var existing={};for(var ei=0;ei<rows.length;ei++)existing[rows[ei].u_type+'|'+rows[ei].u_sub_group]=true;
    var prods2=ft?PRODUCTS.filter(function(p){return p.u_type===ft;}):PRODUCTS;
    prods2=prods2.filter(function(p){return isProdSelected(p.u_type,p.u_sub_group);});
    var rowYear=(rows.length>0)?rows[0].year:'';
    for(var pi2=0;pi2<prods2.length;pi2++){var pk2=prods2[pi2].u_type+'|'+prods2[pi2].u_sub_group;
      if(!existing[pk2]){rows.push({u_type:prods2[pi2].u_type,u_sub_group:prods2[pi2].u_sub_group,month:fm,year:rowYear,litres:0,linetotal:0,realise:0,target_sale:getDefTS(prods2[pi2].u_type,prods2[pi2].u_sub_group),target_realise:getDefTR(prods2[pi2].u_type,prods2[pi2].u_sub_group),_nodata:false});}}
    filtered=rows.sort(function(a,b){return (b.u_type.localeCompare(a.u_type))||((a.target_sale||0)<(b.target_sale||0)?1:(a.target_sale||0)>(b.target_sale||0)?-1:0);});
  }
  renderTable();updateSummary();if(currentSlide===1)await renderSlideTwo();
}

function renderTable(){
  var tbody=document.getElementById('tBody'),html='',db=getDrillBy();
  for(var i=0;i<filtered.length;i++){
    var row=filtered[i],nodata=row._nodata,ts=row.target_sale||0,tr=row.target_realise||0;
    var bc=row.u_type==='PREMIUM'?'badge-prem':'badge-comm';
    var c=nodata?{I:0,J:0,K:0}:calc(row.litres,row.linetotal,row.realise,ts,tr);
    var pk=productKey(row),isOpen=drillOpen[pk];
    var showBtn=db&&dataFetched&&!nodata&&row.litres>0;
    html+='<tr data-i="'+i+'">';
    html+='<td class="num" style="color:var(--text3)">'+(i+1)+'</td>';
    html+='<td><span class="badge '+bc+'">'+row.u_type+'</span></td>';
    if(showBtn){html+='<td class="sub-cell"><button class="drill-btn'+(isOpen?' open':'')+'" onclick="toggleDrill('+i+')">'+(isOpen?'−':'+')+'</button>'+row.u_sub_group+'</td>';}
    else{html+='<td class="sub-cell">'+row.u_sub_group+'</td>';}
    html+='<td class="ecell"><input type="number" value="'+(ts||'')+'" placeholder="0" readonly class="locked"></td>';
    var ar=getAvgRealise(row);
    if(nodata){
      html+='<td class="ecell"><input type="number" value="'+(tr||'')+'" placeholder="0" readonly class="locked"></td>';
      html+='<td class="num" style="color:var(--indigo)">'+(ar?fN(ar,2):'<span class="v-dash">&mdash;</span>')+'</td>';
      html+='<td class="empty-sap">&mdash;</td><td class="empty-sap">&mdash;</td>';
      html+='<td class="empty-sap">&mdash;</td><td class="empty-sap">&mdash;</td><td class="empty-sap">&mdash;</td>';
    }else{
      html+='<td class="ecell"><input type="number" value="'+(tr||'')+'" placeholder="0" readonly class="locked"></td>';
      html+='<td class="num" style="color:var(--indigo)">'+(ar?fN(ar,2):'<span class="v-dash">&mdash;</span>')+'</td>';
      html+='<td class="num">'+fN(row.litres)+'</td>';
      html+='<td class="num">'+fN(row.realise,2)+'</td>';
      html+='<td class="ccell num '+vc(c.I)+'" id="cI'+i+'">'+fN(c.I)+'</td>';
      html+='<td class="ccell num" id="cJ'+i+'">'+(c.J?fN(c.J,2):'<span class="v-dash">&mdash;</span>')+'</td>';
      html+='<td class="ccell num" id="cK'+i+'">'+(c.K?fC(c.K):'<span class="v-dash">&mdash;</span>')+'</td>';
    }
    html+='</tr>';
    if(isOpen&&drillData[pk])html+=renderSubRows(pk,row.u_type,row.u_sub_group,pk,0);
  }
  var t=computeTotals();
  html+='<tr class="total-row"><td></td><td colspan="2">TOTAL</td>';
  html+='<td class="num">'+fN(t.ts)+'</td>';
  html+='<td class="num">'+fN(t.twr,2)+'</td>';
  html+='<td></td>';
  if(!dataFetched){
    html+='<td class="empty-sap">&mdash;</td><td class="empty-sap">&mdash;</td>';
    html+='<td class="empty-sap">&mdash;</td><td></td><td class="empty-sap">&mdash;</td>';
  }else{
    html+='<td class="num">'+fN(t.litres)+'</td>';
    html+='<td class="num">'+fN(t.nr,2)+'</td>';
    html+='<td class="num '+vc(t.cI)+'">'+fN(t.cI)+'</td>';
    html+='<td></td>';
    html+='<td class="num">'+(t.cK?fC(t.cK):'&mdash;')+'</td>';
  }
  html+='</tr>';
  tbody.innerHTML=html;
}

function renderSubRows(productPK,uType,uSubGroup,parentPath,depth){
  var items=drillData[parentPath];if(!items)return'';
  var html='',db=getDrillBy(),parentDimUsed=drillDim[parentPath]||'';
  var usedInChain=usedDimsInPath(parentPath);
  var canExpand=db&&usedInChain.indexOf(db)===-1;
  var indent=20+(depth*20);
  for(var i=0;i<items.length;i++){
    var s=items[i],childPath=parentPath+'>'+parentDimUsed+'='+s.dimension;
    var isChildOpen=drillOpen[childPath];
    var sr=s.litres>0?Math.round((s.linetotal/s.litres)*100)/100:0;
    html+='<tr class="sub-row depth-'+Math.min(depth,2)+'">';
    html+='<td></td><td></td>';
    if(canExpand&&s.litres>0){
      html+='<td style="padding-left:'+indent+'px"><button class="drill-btn'+(isChildOpen?' open':'')+'" onclick="toggleSubDrill(\''+esc(productPK)+'\',\''+esc(uType)+'\',\''+esc(uSubGroup)+'\',\''+esc(childPath)+'\');">'+(isChildOpen?'−':'+')+'</button><span class="sub-dim">'+s.dimension+'</span></td>';
    }else{
      html+='<td style="padding-left:'+indent+'px"><span class="sub-dim">'+s.dimension+'</span></td>';
    }
    html+='<td></td><td></td>';
    var subAr=getDrillAvgRealise(uType,uSubGroup,parentDimUsed,s.dimension);
    html+='<td class="num" style="color:var(--indigo)">'+(subAr?fN(subAr,2):'<span class="v-dash">&mdash;</span>')+'</td>';
    html+='<td class="num">'+fN(s.litres)+'</td>';
    html+='<td class="num">'+fN(sr,2)+'</td>';
    html+='<td></td><td></td><td></td>';
    html+='</tr>';
    if(isChildOpen&&drillData[childPath])html+=renderSubRows(productPK,uType,uSubGroup,childPath,depth+1);
  }
  return html;
}

async function toggleDrill(idx){
  var row=filtered[idx],pk=productKey(row),db=getDrillBy();if(!db)return;
  if(drillOpen[pk]){drillOpen[pk]=false;renderTable();return;}
  var useDb=rootDrillDim||db;
  if(drillData[pk]&&drillDim[pk]===useDb){drillOpen[pk]=true;renderTable();return;}
  clearChildren(pk);delete drillData[pk];delete drillDim[pk];
  if(!rootDrillDim)rootDrillDim=useDb;
  await fetchAndExpand(pk,row.u_type,row.u_sub_group,pk,useDb);
}
async function toggleSubDrill(productPK,uType,uSubGroup,childPath){
  var db=getDrillBy();if(!db)return;
  if(drillOpen[childPath]){drillOpen[childPath]=false;renderTable();return;}
  if(drillData[childPath]&&drillDim[childPath]===db){drillOpen[childPath]=true;renderTable();return;}
  clearChildren(childPath);delete drillData[childPath];delete drillDim[childPath];
  await fetchAndExpand(productPK,uType,uSubGroup,childPath,db);
}
async function fetchAndExpand(productPK,uType,uSubGroup,path,drillBy){
  var filters=filtersFromPath(path);
  var fm=document.getElementById('fMonth').value,fy=document.getElementById('fYear').value;
  var sd=document.getElementById('dFrom').value,ed=document.getElementById('dTo').value;
  var body={start_date:sd,end_date:ed,u_type:uType,u_sub_group:uSubGroup,drill_by:drillBy,filters:filters};
  if(fm)body.month=fm;if(fy)body.year=fy;
  try{
    var res=await fetch(API+'/api/drill-down/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify(body)});
    if(!res.ok){var err=await res.json();throw new Error(err.detail||err.error||'Drill failed');}
    var result=await res.json();
    drillData[path]=result.data.sort(function(a,b){return b.litres-a.litres;});
    drillDim[path]=drillBy;drillOpen[path]=true;renderTable();
    var dl={'U_Main_Group':'groups','State':'states','U_Chain':'chains','ItemName':'items','CardName':'customers'};
    showToast(uSubGroup+' → '+result.data.length+' '+(dl[drillBy]||drillBy),'ok');
  }catch(e){showToast('Drill error: '+e.message,'err');}
}

function calc(litres,lt,realise,ts,tr){var I=litres-ts;var J=0;if(I<0)J=-(((ts*tr)-lt)/I);var K=0;if(litres>ts)K=(realise-tr)*litres;return{I:I,J:J,K:K};}
function computeTotals(){var litres=0,lt=0,ts=0,cI=0,cK=0,spTR=0;for(var i=0;i<filtered.length;i++){var r=filtered[i],rts=r.target_sale||0,rtr=r.target_realise||0;ts+=rts;spTR+=rts*rtr;if(r._nodata)continue;var c=calc(r.litres,r.linetotal,r.realise,rts,rtr);litres+=r.litres;lt+=r.linetotal;cI+=c.I;cK+=c.K;}var twr=ts>0?spTR/ts:0,nr=litres>0?lt/litres:0;return{litres:litres,lt:lt,ts:ts,cI:cI,cK:cK,twr:twr,nr:nr};}

function updateSummary(){
  var t=computeTotals();
  document.getElementById('sTWR').textContent=t.twr?fNp(t.twr,2):'—';
  document.getElementById('sNR').textContent=dataFetched&&t.nr?fNp(t.nr,2):'—';
  document.getElementById('sL').textContent=dataFetched?fNp(t.litres):'—';
  document.getElementById('sT').textContent=fNp(t.ts);
  document.getElementById('sLT').textContent=dataFetched?'₹'+fNp(Math.round(t.lt)):'—';
  var d=document.getElementById('sD');
  if(dataFetched){d.textContent=fNp(t.cI);d.className='sv '+(t.cI>=0?'pos':'neg');}else{d.textContent='—';d.className='sv';}
  document.getElementById('sR').textContent=filtered.length;
}

// ── Slide 2 is independent of slide 1: its own date range, segment & data ──
var sc2Rows=[], sc2Fetched=false, sc2Loading=false, sc2CsvRows=[], sc2Rendered=false;
// Month-level channel rows {u_type,main_group,state,sales_person,u_sub_group,item_name,
// card_name,ym,mlabel,liter} — feeds the month-wise pivot (openChannelMonthly).
// sc2MonthlyType = Premium/Commodity filter; the row hierarchy lives in sc2MonthlyOrder.
var sc2MonthRows=[], sc2MonthlyChannel=null, sc2MonthlyType='';
function getResolvedChannelPeriod(){
  // Monthly targets key off the slide-2 "To" date.
  var toDate=document.getElementById('sc2To').value||new Date().toISOString().split('T')[0];
  var dt=new Date(toDate);
  return {month:dt.getMonth()+1, monthLabel:MONTHS[dt.getMonth()], year:dt.getFullYear()};
}
// Slide 2's Fetch button: same shared loader, just sourced from slide 2's dates.
var sc2Mode='range';   // 'range' = From/To, 'months' = last-N-months
// The Date-Range From/To live here independently of Month-Wise. Month-Wise overwrites the
// shared sc2From/sc2To inputs with its computed last-N-months range, so we snapshot the
// user's own range before leaving Date-Range mode and put it back when we return.
var sc2RangeFrom='', sc2RangeTo='';
function sc2CaptureRangeDates(){
  var f=document.getElementById('sc2From'),t=document.getElementById('sc2To');
  if(f&&f.value)sc2RangeFrom=f.value;
  if(t&&t.value)sc2RangeTo=t.value;
}
function sc2RestoreRangeDates(){
  var f=document.getElementById('sc2From'),t=document.getElementById('sc2To');
  if(f&&sc2RangeFrom)f.value=sc2RangeFrom;
  if(t&&sc2RangeTo)t.value=sc2RangeTo;
}
function setSc2Mode(m){
  if(sc2Mode==='range')sc2CaptureRangeDates();   // remember the Date-Range selection before leaving it
  sc2Mode=m;
  var month=(m==='months');
  document.getElementById('sc2ModeRange').classList.toggle('active',!month);
  document.getElementById('sc2ModeMonths').classList.toggle('active',month);
  document.getElementById('sc2RangeCtl').style.display=month?'none':'contents';
  document.getElementById('sc2MonthCtl').style.display=month?'inline-flex':'none';
  if(!month)sc2RestoreRangeDates();               // back to Date Range → show the user's own dates, not the month range
  // Month-Wise swaps OIH/Bal KPI cards for Top Customers / Top Months.
  var rc=document.querySelectorAll('.sc2-range-card'), mc=document.querySelectorAll('.sc2-month-card'), i;
  for(i=0;i<rc.length;i++)rc[i].style.display=month?'none':'';
  for(i=0;i<mc.length;i++)mc[i].style.display=month?'':'none';
}
async function loadSlideTwoData(){
  if(sc2Mode==='months'){
    var n=parseInt(document.getElementById('sc2Months').value,10);
    if(!n||n<1){showToast('Enter number of months','err');return;}
    if(n>120)n=120;
    var now=new Date();
    document.getElementById('sc2To').value=bevYmd(now);
    document.getElementById('sc2From').value=bevYmd(new Date(now.getFullYear(), now.getMonth()-(n-1), 1));  // 1st of the (n-1)th month back
  }else{
    sc2CaptureRangeDates();   // remember the latest Date-Range selection on every range fetch
  }
  return loadData({source:'slide2'});
}
async function fetchChannelTargets(month,year){
  var key=year+'-'+String(month).padStart(2,'0');
  if(channelTargetCache[key])return channelTargetCache[key];
  var res=await fetch(API+'/api/channel-targets/?month='+month+'&year='+year,{headers:{'X-CSRFToken':getCSRF()}});
  if(!res.ok)throw new Error('Channel target load failed');
  var payload=await res.json();
  channelTargetCache[key]=payload.data||{};
  return channelTargetCache[key];
}
async function fetchSegmentTargets(segment,month,year){
  var seg=sc2Seg(), key=segment+'-'+year+'-'+String(month).padStart(2,'0')+'-'+seg;
  if(segmentTargetCache[key])return segmentTargetCache[key];
  var res=await fetch(API+'/api/segment-targets/?segment='+encodeURIComponent(segment)+'&month='+month+'&year='+year+'&seg='+encodeURIComponent(seg),{headers:{'X-CSRFToken':getCSRF()}});
  if(!res.ok)throw new Error('Segment target load failed');
  var payload=await res.json();
  segmentTargetCache[key]=payload.data||{};
  return segmentTargetCache[key];
}
function getSlideTwoDrillBy(){
  var el=document.getElementById('sc2DrillBy');
  return el?el.value:'main_group';
}
function getSlideTwoGroupFilter(){
  var el=document.getElementById('sc2DrillGroup');
  return el?String(el.value||'').trim().toUpperCase():'';
}
function onSlideTwoDrillChange(){
  var mode=getSlideTwoDrillBy();
  var showDyn=mode!=='main_group';
  var wrap=document.getElementById('sc2DynWrap');
  if(wrap)wrap.style.display=showDyn?'':'none';
  // Seed the dynamic driller from the chosen mode (Person / State); the panel
  // then lets the user reorder / add dimensions on top of that.
  if(showDyn){ sc2DynUserSet=false; sc2DynOrder=[mode]; sc2DynChosen=sc2DynOrder.slice(); sc2DynExpanded={}; sc2DynUpdateLabel(); }
  renderSlideTwo();
}
function normaliseChannelGroup(name){
  var upper=String(name||'').trim().toUpperCase();
  for(var i=0;i<CHANNEL_BLOCKS.length;i++){
    if(CHANNEL_BLOCKS[i].members.indexOf(upper)!==-1)return CHANNEL_BLOCKS[i].name;
  }
  return '';
}
function getFilteredChannelRows(){
  // Slide 2's own data, filtered only by its own Segment (Premium/Commodity/All).
  var seg=document.getElementById('sc2Segment')?document.getElementById('sc2Segment').value:'';
  var rows=[];
  for(var i=0;i<sc2Rows.length;i++){
    var row=sc2Rows[i];
    if(seg&&row.u_type!==seg)continue;
    rows.push(row);
  }
  return rows;
}
/* Fills one "<rate>/LTR - Rs<money>" line under a KPI figure. Done Ltr and OIH Ltr
   both use it, so the two lines can never drift apart.
     el       the small div under the litres
     litres   the litres that line describes
     perLitre the realise rate for them (money / litres), worked out by the caller
     what     wording for the tooltip, e.g. "Done" or "open-order"
   No Rs on the rate - the "/LTR" already says what it is; the money keeps its Rs. */
function setRealiseLine(el,litres,perLitre,what,emptyText){
  if(!el)return;
  var rate=(perLitre&&isFinite(perLitre))?Number(perLitre):0;
  if(rate!==0){
    /* Left = the realise itself (₹ per litre), set large. Right = what those litres
       add up to. The word TOTAL is there because readers could not tell which of the
       two numbers was "the realise"; the ₹ makes the rate read as money at a glance. */
    el.innerHTML='<span class="k-rate">₹'+fNp(rate,2)+'<span class="u">/LTR</span></span>'
                +'<span class="k-money"><span class="u">TOTAL</span> '+fRsShort((litres||0)*rate)+'</span>';
    el.title='Realise of the '+what+' litres. Left = the realise itself, ₹ per litre. '
            +'Right = the total those litres come to (rate × litres).';
  }else if((litres||0)!==0){        // a balance can be negative, so test for 'not zero'
    // Litres are there but no value came with them, so a rate cannot be worked out. Say
    // so rather than showing a bare dash, which looks like the figure failed to load.
    el.innerHTML='<span class="u">'+(emptyText||'no revenue data')+'</span>';
    el.title='These '+what+' litres carry no value, so a ₹ per litre realise cannot be calculated';
  }else{
    el.innerHTML='&mdash;';
    el.title='';
  }
}
function setSlideTwoKpis(targetLtr,doneLtr,targetRealise,currentRealise,oihLtr,balLtr,oihRealise){
  // Bal Ltr mirrors the table's primary "Bal" column = (effective target − Done − OIH). Callers
  // pass balLtr explicitly when the table uses a flex-adjusted target; otherwise default to
  // Target − Done − OIH so the KPI strip stays consistent with the per-row / TOTAL "Bal".
  var bal=(balLtr===undefined||balLtr===null)?((targetLtr||0)-(doneLtr||0)-(oihLtr||0)):balLtr;
  var elTarget=document.getElementById('sc2KpiTarget');
  var elDone=document.getElementById('sc2KpiDone');
  var elOih=document.getElementById('sc2KpiOih');
  var elBal=document.getElementById('sc2KpiBal');
  /* The old "Actual Realise" card is gone - its figure is the ₹/LTR half of the
     realise line under Done Ltr now. The lookup stays (and is guarded) so nothing
     breaks if that card is ever put back. */
  var elCurrentRealise=document.getElementById('sc2KpiCurrentRealise');
  if(elTarget)elTarget.innerHTML=fN(targetLtr||0);
  if(elDone)elDone.innerHTML=fN(doneLtr||0);
  if(elOih)elOih.innerHTML=fN(oihLtr||0);
  if(elBal){
    elBal.innerHTML=fN(bal);
    elBal.className=bal>=0?'pos':'neg';
  }
  if(elCurrentRealise)elCurrentRealise.innerHTML=(currentRealise&&isFinite(currentRealise))?'₹'+fNp(currentRealise,2):'&mdash;';
  /* The realise line under a litres figure, shown two ways:
       rate  = ₹ per litre
       money = rate × litres = what those litres come to.
     Done uses line_total ÷ litres (sc2Realise) — the figure the old "Actual Realise"
     card used to show on its own. OIH uses the same shape on open orders:
     open_value ÷ open_qty. */
  setRealiseLine(document.getElementById('sc2KpiTargetRealise'), targetLtr, targetRealise, 'Target', 'no target realise');
  setRealiseLine(document.getElementById('sc2KpiDoneRealise'),   doneLtr,   currentRealise, 'Done');
  setRealiseLine(document.getElementById('sc2KpiOihRealise'),    oihLtr,    oihRealise,     'open-order');
  /* Bal realise = (target revenue − done revenue) ÷ balance litres: the ₹ per litre
     still needed on what is left in order to reach the target revenue. This is the
     SAME formula the "Bal Realise" column in the tables uses, so the card and the
     table can never disagree. It only means anything when a target exists - with no
     target there is no target revenue to measure the balance against.
     Its TOTAL works out to (target revenue − done revenue): the money still to earn. */
  var balRealise=((targetLtr||0)>0&&bal!==0)
    ? (((targetLtr||0)*(targetRealise||0))-((doneLtr||0)*(currentRealise||0)))/bal : 0;
  setRealiseLine(document.getElementById('sc2KpiBalRealise'), bal, balRealise,
                 'balance', 'needs a target');

  sc2RenderTopKpis();
}
// ── Slide 2 Top Customers / Top Months (by Done litres; honours the Segment filter) ──
function sc2TopCustomers(){
  var seg=sc2Seg(), map={}, order=[];
  (sc2Rows||[]).forEach(function(r){
    if(seg&&r.u_type!==seg)return;
    var k=r.card_name||'—'; if(!map[k]){map[k]={name:k,litres:0};order.push(k);}
    map[k].litres+=Number(r.liter)||0;
  });
  return order.map(function(k){return map[k];}).sort(function(a,b){return b.litres-a.litres||String(a.name).localeCompare(String(b.name));});
}
function sc2TopMonths(){
  var seg=sc2Seg(), map={}, order=[];
  (allData||[]).forEach(function(r){
    if(seg&&r.u_type!==seg)return;
    var ym=(r.year||'')+'-'+(r.month||''), label=((r.month||'')+' '+(r.year||'')).trim();
    if(!map[ym]){map[ym]={name:label||'—',litres:0};order.push(ym);}
    map[ym].litres+=Number(r.litres)||0;
  });
  return order.map(function(k){return map[k];}).sort(function(a,b){return b.litres-a.litres||String(a.name).localeCompare(String(b.name));});
}
function sc2RenderTopKpis(){
  var tc=sc2TopCustomers(), tm=sc2TopMonths();
  var ec=document.getElementById('sc2KpiTopCust'); if(ec)ec.textContent=tc.length?tc[0].name:'—';
  var em=document.getElementById('sc2KpiTopMonth'); if(em)em.textContent=tm.length?tm[0].name:'—';
}
function openSc2Top(which){
  if(!sc2Fetched){showToast('Fetch the channel dashboard first','info');return;}
  var isCust=(which==='customers'), list=isCust?sc2TopCustomers():sc2TopMonths();
  document.getElementById('sc2TopTitle').textContent=(isCust?'Top Customers':'Top Months')+(sc2Seg()?(' — '+sc2Seg()):'');
  document.getElementById('sc2TopNameHead').textContent=isCust?'Customer':'Month';
  var tl=0; for(var i=0;i<list.length;i++)tl+=list[i].litres;
  document.getElementById('sc2TopSub').textContent=list.length+(isCust?' customer(s) · ':' month(s) · ')+fN(Math.round(tl))+' done litres — highest first';
  var body=document.getElementById('sc2TopBody');
  if(!list.length){
    body.innerHTML='<tr><td colspan="3" style="padding:34px;text-align:center;color:#7b8794">No data'+(sc2Seg()?(' for '+esc(sc2Seg())):'')+'.</td></tr>';
  }else{
    var html='';
    for(var k=0;k<list.length;k++){var r=list[k]; html+='<tr><td>'+(k+1)+'</td><td>'+esc(r.name)+'</td><td class="num">'+fN(Math.round(r.litres))+'</td></tr>';}
    html+='<tr class="cd-total"><td colspan="2">TOTAL</td><td class="num">'+fN(Math.round(tl))+'</td></tr>';
    body.innerHTML=html;
  }
  document.getElementById('sc2TopModal').classList.add('show');
}
function closeSc2Top(){document.getElementById('sc2TopModal').classList.remove('show');}
// Compare popup: Premium/Commodity (rows) × months (columns), done litres, months by sales desc.
function sc2CompareMatrix(){
  var months={}, order=[], cells={PREMIUM:{},COMMODITY:{}};
  (allData||[]).forEach(function(r){
    var ut=String(r.u_type||'').toUpperCase(); if(ut!=='PREMIUM'&&ut!=='COMMODITY')return;
    var ym=(r.year||'')+'-'+(r.month||''), label=((r.month||'')+' '+(r.year||'')).trim();
    if(!months[ym]){months[ym]={ym:ym,label:label||'—',total:0};order.push(ym);}
    var v=Number(r.litres)||0; months[ym].total+=v; cells[ut][ym]=(cells[ut][ym]||0)+v;
  });
  return {months:order.map(function(k){return months[k];}).sort(function(a,b){return b.total-a.total;}), cells:cells};
}
function openSc2Compare(){
  if(!sc2Fetched){showToast('Fetch the channel dashboard first','info');return;}
  var m=sc2CompareMatrix(), months=m.months, cells=m.cells;
  document.getElementById('sc2CompareSub').textContent=months.length+' month(s) · done litres · highest-selling month first';
  document.getElementById('sc2CompareHead').innerHTML='<tr><th>Segment</th>'+months.map(function(mo){return '<th class="num">'+esc(mo.label)+'</th>';}).join('')+'<th class="num">Total</th></tr>';
  function row(type){
    var rt=0, tds=months.map(function(mo){var v=cells[type][mo.ym]||0;rt+=v;return '<td class="num">'+(v?fN(Math.round(v)):'')+'</td>';}).join('');
    return '<tr><td>'+type.charAt(0)+type.slice(1).toLowerCase()+'</td>'+tds+'<td class="num">'+fN(Math.round(rt))+'</td></tr>';
  }
  var colTot=0, totTds=months.map(function(mo){var v=(cells.PREMIUM[mo.ym]||0)+(cells.COMMODITY[mo.ym]||0);colTot+=v;return '<td class="num">'+fN(Math.round(v))+'</td>';}).join('');
  document.getElementById('sc2CompareBody').innerHTML=row('PREMIUM')+row('COMMODITY')
    +'<tr class="cd-total"><td>TOTAL</td>'+totTds+'<td class="num">'+fN(Math.round(colTot))+'</td></tr>';
  document.getElementById('sc2CompareModal').classList.add('show');
}
function closeSc2Compare(){document.getElementById('sc2CompareModal').classList.remove('show');}
// ===== OILS Month-Wise: per-channel pivot — dynamic multi-dimension rows × Months =====
// The Drill By panel (reused from the date-range modal) picks an ORDERED set of dimensions
// (State / Contact Person / Product / Item Name / Customer, + Main Group for REST). Rows become
// an expandable hierarchy in that order, months are the columns, plus a Grand Total. Every value
// drills to the customers behind that node × month. Honours the modal's Type selector.
var sc2MonthlyOrder=['state'], sc2MonthlyChosen=['state'], sc2mExpanded={}, sc2mNodeByPath={};
// Row bucket value for one row under one dimension — mirrors the date-range modal's keys.
function sc2MonthlyRowVal(r,dim){
  var g=String(r.main_group||'').toUpperCase();
  if(dim==='group')return g||'—';
  if(dim==='person')return assignedPerson(g,r.state)||'—';
  if(dim==='product')return String(r.u_sub_group||'').trim().toUpperCase()||'—';
  if(dim==='item')return String(r.item_name||'').trim().toUpperCase()||'—';
  if(dim==='customer')return String(r.card_name||'').trim().toUpperCase()||'—';
  return String(r.state||'').trim().toUpperCase()||'UNKNOWN';
}
// Month rows for the active channel + Type, and the sorted month-column list.
function sc2MonthlyActiveRows(){
  var members=channelMembers(sc2MonthlyChannel), type=sc2MonthlyType, out=[], months={};
  for(var i=0;i<sc2MonthRows.length;i++){var r=sc2MonthRows[i];
    if(members.indexOf(String(r.main_group||'').toUpperCase())===-1)continue;
    if(type&&String(r.u_type||'').toUpperCase()!==type)continue;
    if(!r.ym)continue;
    if(!months[r.ym])months[r.ym]={ym:r.ym,label:r.mlabel||r.ym};
    out.push(r);
  }
  return {rows:out, months:Object.keys(months).sort().map(function(k){return months[k];})};
}
// Recursive hierarchy: group rows by the dimension order, summing litres per month at each node.
function sc2mBuildTree(rows,dims,depth,prefix,pfilt,pcrumb){
  if(depth>=dims.length)return [];
  var dim=dims[depth], map={}, order=[];
  for(var i=0;i<rows.length;i++){var r=rows[i], key=sc2MonthlyRowVal(r,dim);
    if(!map[key]){map[key]={label:key,dim:dim,by:{},total:0,_rows:[]};order.push(key);}
    var v=Number(r.liter)||0; map[key].by[r.ym]=(map[key].by[r.ym]||0)+v; map[key].total+=v; map[key]._rows.push(r);
  }
  return order.map(function(k){var n=map[k];
    n.path=prefix+'>'+dim+':'+k;
    n.filters={}; for(var fk in pfilt)n.filters[fk]=pfilt[fk]; n.filters[dim]=k;
    n.crumb=pcrumb?(pcrumb+' › '+k):k;
    sc2mNodeByPath[n.path]=n;
    n.children=sc2mBuildTree(n._rows,dims,depth+1,n.path,n.filters,n.crumb);
    n.leaf=!n.children.length; delete n._rows;
    return n;
  }).sort(function(a,b){return b.total-a.total||String(a.label).localeCompare(String(b.label));});
}
function sc2mTreeRows(nodes,months,depth){
  var html='';
  nodes.forEach(function(n){
    var hasKids=!n.leaf, open=!!sc2mExpanded[n.path], pad='padding-left:'+(12+depth*18)+'px';
    var tw=hasKids?('<span class="cd-twirl'+(open?' open':'')+'">&#9654;</span>'):'<span class="cd-twirl cd-leaf"></span>';
    var nameCell=hasKids
      ? '<td class="gc-name sc2m-parent" data-sc2mtoggle="'+esc(n.path)+'" style="cursor:pointer;'+pad+'">'+tw+esc(n.label)+'</td>'
      : '<td class="gc-name" style="'+pad+'">'+tw+esc(n.label)+'</td>';
    var cells=months.map(function(mo){var v=n.by[mo.ym]||0;
      return '<td class="num">'+(v?fN(Math.round(v)):'')+'</td>';
    }).join('');
    var grand='<td class="num sc2-grand">'+fN(Math.round(n.total))+'</td>';
    html+='<tr>'+nameCell+cells+grand+'</tr>';
    if(hasKids&&open)html+=sc2mTreeRows(n.children,months,depth+1);
  });
  return html;
}
function openChannelMonthly(name){
  if(!sc2Fetched){showToast('Fetch the channel dashboard first','info');return;}
  sc2MonthlyChannel=name;
  sc2MonthlyType=sc2Seg();                          // seed from the toolbar segment
  sc2MonthlyOrder=(name==='REST')?['group']:['state'];
  sc2MonthlyChosen=sc2MonthlyOrder.slice();
  sc2mExpanded={};
  var typeSel=document.getElementById('sc2mType'); if(typeSel)typeSel.value=sc2MonthlyType;
  updateSc2mDrillLabel(); renderSc2mDpList();
  var accent=(typeof CHANNEL_COLORS!=='undefined'&&CHANNEL_COLORS[name])||{band:'#2563eb',ring:'#1d4ed8'};
  var hdr=document.getElementById('sc2MonthlyHeader');
  hdr.style.setProperty('--accent',accent.band); hdr.style.setProperty('--accent-strong',accent.ring);
  var t=(typeof themedChannelTitle==='function'&&themedChannelTitle(name))||name;
  document.getElementById('sc2MonthlyTitle').textContent=name+' — '+String(t).replace(/ Performance| Tracking| Market Trends/g,'')+' · Month-Wise';
  sc2MonthlyRender();
  document.getElementById('sc2MonthlyModal').classList.add('show');
}
function sc2MonthlyTypeChange(v){sc2MonthlyType=v;sc2MonthlyRender();}
function sc2MonthlyRender(){
  sc2mNodeByPath={};
  var a=sc2MonthlyActiveRows(), months=a.months;
  var dims=sc2MonthlyOrder.length?sc2MonthlyOrder:['state'];
  var tree=sc2mBuildTree(a.rows,dims,0,'',{},'');
  var rowLabel=dims.map(function(d){return dimMeta(d).label;}).join(' › ');
  document.getElementById('sc2MonthlySub').textContent=tree.length+' '+dimMeta(dims[0]).label.toLowerCase()+'(s) · '+months.length+' month(s) · Done litres'+(sc2MonthlyType?(' · '+sc2MonthlyType):'')+(dims.length>1?' · expand rows to drill':'');
  document.getElementById('sc2MonthlyHead').innerHTML='<tr><th class="gc-name">'+esc(rowLabel)+'</th>'
    +months.map(function(mo){return '<th class="num">'+esc(mo.label)+'</th>';}).join('')
    +'<th class="num sc2-grand">Grand Total</th></tr>';
  var body=document.getElementById('sc2MonthlyBody');
  if(!tree.length){
    body.innerHTML='<tr><td colspan="'+(months.length+2)+'" style="padding:34px;text-align:center;color:#7b8794">No sales in this selection'+(sc2MonthlyType?(' for '+esc(sc2MonthlyType)):'')+'.</td></tr>';
    return;
  }
  var html=sc2mTreeRows(tree,months,0);
  var colTot={}, gtot=0;
  tree.forEach(function(n){for(var ym in n.by)colTot[ym]=(colTot[ym]||0)+n.by[ym]; gtot+=n.total;});
  var totCells=months.map(function(mo){return '<td class="num">'+fN(Math.round(colTot[mo.ym]||0))+'</td>';}).join('');
  html+='<tr class="cd-total"><td class="gc-name">TOTAL</td>'+totCells+'<td class="num sc2-grand">'+fN(Math.round(gtot))+'</td></tr>';
  body.innerHTML=html;
}
function closeSc2Monthly(){document.getElementById('sc2MonthlyModal').classList.remove('show');}
// ── Drill By panel (multi-select, ordered) — reuses the date-range modal's dim list/CSS ──
function sc2mDimsFor(){return detailDimsFor(sc2MonthlyChannel);}
function updateSc2mDrillLabel(){
  document.getElementById('sc2mDrillLabel').textContent=sc2MonthlyOrder.length?sc2MonthlyOrder.map(function(k){return dimMeta(k).label;}).join(' › '):'— none —';
}
function renderSc2mDpList(){
  var list=document.getElementById('sc2mDpList'); if(!list)return; list.innerHTML='';
  sc2mDimsFor().forEach(function(d){
    var pos=sc2MonthlyChosen.indexOf(d.key);
    var item=document.createElement('label');
    item.className='cd-dp-item'+(pos!==-1?' checked':'');
    item.innerHTML='<input type="checkbox" '+(pos!==-1?'checked':'')+'>'
      +'<span class="cd-dp-name">'+d.name+'<div class="cd-dp-sub">'+d.sub+'</div></span>'
      +'<span class="cd-dp-pos">'+(pos!==-1?pos+1:'')+'</span>';
    item.querySelector('input').addEventListener('change',function(e){
      if(e.target.checked){if(sc2MonthlyChosen.indexOf(d.key)===-1)sc2MonthlyChosen.push(d.key);}
      else{sc2MonthlyChosen=sc2MonthlyChosen.filter(function(k){return k!==d.key;});}
      renderSc2mDpList();
    });
    list.appendChild(item);
  });
}
function toggleSc2mDrill(e){e.stopPropagation();sc2MonthlyChosen=sc2MonthlyOrder.slice();renderSc2mDpList();document.getElementById('sc2mDrillPanel').classList.toggle('open');}
function sc2mSelectAll(){sc2MonthlyChosen=sc2mDimsFor().map(function(d){return d.key;});renderSc2mDpList();}
function sc2mClearAll(){sc2MonthlyChosen=[];renderSc2mDpList();}
function applySc2mDrill(){
  if(!sc2MonthlyChosen.length){alert('Select at least one drill dimension.');return;}
  sc2MonthlyOrder=sc2MonthlyChosen.slice();
  sc2mExpanded={};
  document.getElementById('sc2mDrillPanel').classList.remove('open');
  updateSc2mDrillLabel(); sc2MonthlyRender();
}
document.addEventListener('click',function(){var p=document.getElementById('sc2mDrillPanel');if(p)p.classList.remove('open');});
// Delegated clicks inside the pivot body: parent name → expand/collapse. (Per-cell customer
// drill was removed — the Drill By panel now covers customer breakdown directly.)
document.addEventListener('click',function(e){
  if(!e.target||!e.target.closest)return;
  var par=e.target.closest('#sc2MonthlyBody td.sc2m-parent[data-sc2mtoggle]');
  if(par){var p=par.getAttribute('data-sc2mtoggle');sc2mExpanded[p]=!sc2mExpanded[p];sc2MonthlyRender();}
});
// Customer drill for one node × month (blank ym = all months) — filtered by the node's full path.
function openSc2MonthlyCust(path,ym,mlabel){
  var node=sc2mNodeByPath[path]; if(!node)return;
  var filters=node.filters||{}, members=channelMembers(sc2MonthlyChannel), type=sc2MonthlyType;
  var map={}, order=[];
  for(var i=0;i<sc2MonthRows.length;i++){var r=sc2MonthRows[i];
    if(members.indexOf(String(r.main_group||'').toUpperCase())===-1)continue;
    if(type&&String(r.u_type||'').toUpperCase()!==type)continue;
    if(ym&&r.ym!==ym)continue;
    var ok=true; for(var dk in filters){if(sc2MonthlyRowVal(r,dk)!==filters[dk]){ok=false;break;}}
    if(!ok)continue;
    var cust=String(r.card_name||'').trim().toUpperCase()||'—';
    if(!map[cust]){map[cust]={name:cust,liter:0};order.push(cust);}
    map[cust].liter+=Number(r.liter)||0;
  }
  var list=order.map(function(k){return map[k];}).sort(function(a,b){return b.liter-a.liter||String(a.name).localeCompare(String(b.name));});
  var tot=0; for(var j=0;j<list.length;j++)tot+=list[j].liter;
  document.getElementById('sc2McTitle').textContent=(node.crumb||node.label)+' · '+mlabel;
  document.getElementById('sc2McSub').textContent=list.length+' customer(s) · '+fN(Math.round(tot))+' litres'+(sc2MonthlyType?(' · '+sc2MonthlyType):'');
  var b=document.getElementById('sc2McBody');
  if(!list.length){
    b.innerHTML='<tr><td colspan="3" style="padding:30px;text-align:center;color:#7b8794">No customers.</td></tr>';
  }else{
    var h=''; for(var k=0;k<list.length;k++){h+='<tr><td>'+(k+1)+'</td><td>'+esc(list[k].name)+'</td><td class="num">'+fN(Math.round(list[k].liter))+'</td></tr>';}
    h+='<tr class="cd-total"><td colspan="2">TOTAL</td><td class="num">'+fN(Math.round(tot))+'</td></tr>';
    b.innerHTML=h;
  }
  document.getElementById('sc2MonthlyCustModal').classList.add('show');
}
function closeSc2MonthlyCust(){document.getElementById('sc2MonthlyCustModal').classList.remove('show');}
// Today's Sales popup — fetches just today's range and splits done litres Premium vs Commodity.
async function openTodaySales(){
  var today=bevYmd(new Date());
  document.getElementById('sc2TodaySub').textContent='Done litres · '+today;
  document.getElementById('sc2TodayBody').innerHTML='<div class="sc2-today-msg">Loading…</div>';
  document.getElementById('sc2TodayModal').classList.add('show');
  try{
    var res=await fetch(API+'/api/sales-data/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify({start_date:today,end_date:today})});
    if(!res.ok){var e=await res.json();throw new Error(e.detail||e.error||'Server error');}
    var data=(await res.json()).data||[], prem=0, comm=0;
    for(var i=0;i<data.length;i++){
      var ut=String(data[i].u_type||'').toUpperCase(), l=Number(data[i].litres)||0;
      if(ut==='PREMIUM')prem+=l; else if(ut==='COMMODITY')comm+=l;
    }
    document.getElementById('sc2TodayBody').innerHTML=
      '<div class="sc2-today-row prem"><span class="stl">Premium</span><span class="sv">'+fN(Math.round(prem))+' L</span></div>'
      +'<div class="sc2-today-row comm"><span class="stl">Commodity</span><span class="sv">'+fN(Math.round(comm))+' L</span></div>'
      +'<div class="sc2-today-row total"><span class="stl">Total</span><span class="sv">'+fN(Math.round(prem+comm))+' L</span></div>';
  }catch(err){
    document.getElementById('sc2TodayBody').innerHTML='<div class="sc2-today-msg">Could not load today\'s sales: '+esc(err.message||'error')+'</div>';
  }
}
function closeTodaySales(){document.getElementById('sc2TodayModal').classList.remove('show');}
function buildSlideTwoDrillRows(rows,dimension,groupFilter,targetNodes,oihRows){
  var isPerson=dimension==='person', map={};
  function cell(name){ if(!map[name])map[name]={name:name,target:0,done:0,oih:0,lineTotal:0,trWsum:0,trW:0}; return map[name]; }
  for(var i=0;i<rows.length;i++){
    var row=rows[i], rg=normaliseChannelGroup(row.u_main_group);
    if(groupFilter && rg!==groupFilter)continue;
    var name=isPerson?assignedPerson(row.u_main_group,row.state):(String(row.state||'UNKNOWN').trim().toUpperCase()||'UNKNOWN');
    if(isPerson && !name)continue;
    var c=cell(name); c.done+=Number(row.liter)||0; c.lineTotal+=Number(row.line_total)||0;
  }
  if(isPerson){ for(var ai=0;ai<ASSIGNED_PERSONS.length;ai++)cell(ASSIGNED_PERSONS[ai]); }
  for(var t=0;t<(targetNodes||[]).length;t++){
    var n=targetNodes[t], ng=String(n.main_group||'').toUpperCase();
    if(groupFilter && normaliseChannelGroup(ng)!==groupFilter)continue;
    var key=isPerson?(String(n.sales_person||'').toUpperCase()||assignedPerson(ng,n.state)):(String(n.state||'').toUpperCase());
    if(!key)continue;
    var c2=cell(key), tl=Number(n.target_ltrs)||0, tr=Number(n.target_realise)||0;
    c2.target+=tl;
    if(tr>0){ var wt=tl>0?tl:1; c2.trWsum+=tr*wt; c2.trW+=wt; }
  }
  var oihSeg=sc2Seg();
  for(var o=0;o<(oihRows||[]).length;o++){
    var x=oihRows[o], xg=String(x.main_group||'').toUpperCase();
    if(groupFilter && normaliseChannelGroup(xg)!==groupFilter)continue;
    if(oihSeg&&String(x.u_type||'').toUpperCase()!==oihSeg)continue;   // honour segment
    var k3=isPerson?String(x.sales_person||'').toUpperCase():String(x.state||'').toUpperCase();
    if(!k3)continue;
    cell(k3).oih+=Number(x.open_qty)||0;
  }
  var list=Object.keys(map).map(function(k){
    var c=map[k];
    c.targetRealise=c.trW>0?c.trWsum/c.trW:0;
    c.actualRealise=c.done>0?c.lineTotal/c.done:0;
    return c;
  });
  list.sort(function(a,b){return b.done-a.done||a.name.localeCompare(b.name);});
  return list;
}
// ── Flexible View (slide-2 grid tables) ───────────────────────────────────────
// The toolbar VIEW selector (#cdView, shared with the channel-detail modal) drives
// cdViewMode. In 'flex' mode the person/state drill and the Commodity view gain two
// extra grid columns — an editable Flex TGT and a computed Dent (Target − Flex) — just
// like the modal. Values are scratch, kept per row-path in sc2FlexStore.
var sc2FlexStore={};
function sc2FlexOn(){return cdViewMode==='flex';}
function sc2FlexHeadCols(){return sc2FlexOn()?'<div class="sc2-tt-col">Flex TGT</div><div class="sc2-tt-col">Dent</div>':'';}
/* name + Target/Tgt + Done/Done + OIH/OIH + Bal/Bal = 9 tracks; Flex View inserts
   Flex TGT and Dent after Target L, making 11. */
function sc2DynGcols(){return sc2FlexOn()
  ?'grid-template-columns:1.4fr .7fr .62fr .62fr .8fr .66fr .8fr .74fr .8fr .7fr .8fr .8fr'
  :'grid-template-columns:1.45fr .7fr .82fr .7fr .82fr .76fr .82fr .74fr .82fr .82fr';}
function comGcols(){return sc2FlexOn()
  ?'grid-template-columns:1.5fr .76fr .7fr .7fr .76fr .8fr .8fr .76fr .76fr .85fr .8fr .85fr'
  :'grid-template-columns:1.5fr .76fr .76fr .8fr .8fr .76fr .76fr .85fr .8fr .85fr';}
// Two grid cells: the Flex TGT input + the Dent. editable===false (e.g. commodity rows
// with no product-level target) renders an em-dash pair so the grid stays aligned. The
// input carries the row's Done/OIH/rate/revenue so the handler can recompute Bal live.
function sc2FlexCells(key,row,editable){
  if(editable===false)return '<div class="sc2-drillval sc2-flexcol">&mdash;</div><div class="sc2-drillval sc2-flexcol cdent">&mdash;</div>';
  var tgt=Number(row.target)||0,has=sc2FlexStore.hasOwnProperty(key),fv=has?sc2FlexStore[key]:null;
  var dent=has?(tgt-fv):NaN,dCls=isFinite(dent)?(dent>0?'sc2-dent-pos':(dent<0?'sc2-dent-neg':'')):'';
  return '<div class="sc2-drillval sc2-flexcol">'
      +'<input type="number" class="sc2-flex-input" step="any" inputmode="decimal"'
      +' data-flexkey="'+esc(key)+'" data-tgt="'+tgt+'" data-done="'+(Number(row.done)||0)+'" data-oih="'+(Number(row.oih)||0)+'"'
      +' data-tr="'+(Number(row.targetRealise)||0)+'" data-lt="'+(Number(row.lineTotal)||0)+'"'
      +' placeholder="'+fN(tgt)+'" value="'+(has?String(fv):'')+'"></div>'
    +'<div class="sc2-drillval sc2-flexcol cdent '+dCls+'">'+(isFinite(dent)?fN(dent):'&mdash;')+'</div>';
}
function drillCells(c,flexKey){
  // Effective target = the flexible override ONLY while Flex View is on (its Flex TGT column
  // is visible then); otherwise the real target, so Bal matches the shown "Tgt L". Saved flex
  // values live in sc2FlexStore even when Flex is off, so gate on sc2FlexOn() or Bal leaks them.
  var eff=(sc2FlexOn()&&flexKey&&sc2FlexStore.hasOwnProperty(flexKey))?sc2FlexStore[flexKey]:(c.target||0);
  var bal=eff-((c.done||0)+(c.oih||0)), balClass=bal>=0?'sc2-bal-good':'sc2-bal-bad';
  /* OIH Realise and Bal Realise use exactly the formulas the channel cards use
     (see cardCells), so a drill row and its card can never disagree:
       OIH Realise = open-order value / open litres
       Bal Realise = (target revenue − done revenue) / balance litres
     Both are ₹ PER LITRE - not a litres subtraction. */
  var oihRlz=(c.oih||0)>0?((c.oihLineTotal||0)/c.oih):NaN;
  var oihRlzStr=isFinite(oihRlz)?'₹'+fNp(oihRlz,2):'&mdash;';
  var balRlz=(eff>0&&bal!==0)?((eff*(c.targetRealise||0))-(c.lineTotal||0))/bal:NaN;
  var balRlzStr=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';
  var balWo=eff-(c.done||0), balWoClass=balWo>=0?'sc2-bal-good':'sc2-bal-bad';   // Target − Done (no OIH)
  /* Tgt Realise and Done Realise are stored figures, so a real zero prints as ₹0.00 -
     never a dash. OIH Realise and Bal Realise are divisions, so they DO keep the dash
     when there is nothing to divide by: "cannot be worked out" is not the same as zero. */
  var tgtRlz=Number(c.targetRealise)||0;
  var actRlz=Number(c.actualRealise)||0;
  return '<div class="sc2-drillval">'+fN(c.target||0)+'</div>'
    +(sc2FlexOn()&&flexKey?sc2FlexCells(flexKey,c):'')
    +'<div class="sc2-drillval">₹'+fNp(tgtRlz,2)+'</div>'
    +'<div class="sc2-drillval">'+fN(c.done||0)+'</div>'
    +'<div class="sc2-drillval">₹'+fNp(actRlz,2)+'</div>'
    +'<div class="sc2-drillval">'+fN(c.oih||0)+'</div>'
    +'<div class="sc2-drillval">'+oihRlzStr+'</div>'
    +'<div class="sc2-drillval sc2-bal-cell '+balClass+'">'+fN(bal)+'</div>'
    +'<div class="sc2-drillval sc2-balwo-cell '+balWoClass+'">'+fN(balWo)+'</div>'
    +'<div class="sc2-drillval sc2-balrlz-cell">'+balRlzStr+'</div>';
}
function renderSlideTwoDrillTable(rows,dimension,groupFilter){
  var isPerson=dimension==='person';
  var title=(isPerson?'Sales Person':'State')+' wise';
  if(groupFilter)title+=' — '+groupFilter;
  var gs=' style="grid-template-columns:1.4fr .8fr .8fr .8fr .8fr .9fr .9fr"';
  var html='<div class="sc2-drillcard sc2-drillfull">'
    +'<div class="sc2-thead sc2-wide"'+gs+'>'
      +'<div class="sc2-tt-col" style="text-align:left">'+esc(title)+'</div>'
      +'<div class="sc2-tt-col">Tgt L</div><div class="sc2-tt-col">Done L</div>'
      +'<div class="sc2-tt-col">OIH</div><div class="sc2-tt-col">Bal</div>'
      +'<div class="sc2-tt-col">Tgt Rlz</div><div class="sc2-tt-col">Done Rlz</div>'
    +'</div>'
    +'<div class="sc2-drillbody">';
  if(!rows.length){
    html+='<div class="sc2-state-empty">No rows available in the current selection.</div>';
  }else{
    for(var i=0;i<rows.length;i++){
      html+='<div class="sc2-drillrow sc2-wide"'+gs+'><div class="sc2-drillname">'+esc(rows[i].name)+'</div>'+drillCells(rows[i])+'</div>';
    }
    var tot=cardRowsTotal(rows); tot.name='TOTAL';
    html+='<div class="sc2-drillrow sc2-wide sc2-state-total"'+gs+'><div class="sc2-drillname">TOTAL</div>'+drillCells(tot)+'</div>';
  }
  html+='</div></div>';
  return html;
}

/* ===== COMMODITY VIEW (segment = Commodity) =====
   Commodity is owned end-to-end by one sales person (Surjeet Singh). Instead of
   channel cards we show a product table (Target/Tgt-Realise from the per-product
   MonthlyTarget store that Slide 1 uses; Done/Actual from the SAP rows). The drill
   order (Product / Main Group / State) is user-configurable via a panel like the
   Update Targets page. Targets are a per-product property so they render on product
   rows; the KPI cards & TOTAL row use distinct-product totals so they stay correct
   in any drill order. */
var COMMODITY_PERSON='Surjeet Singh';
var comExpanded={};
var COM_DIMS=[{key:'product',name:'Product'},{key:'main_group',name:'Main Group'},{key:'state',name:'State'},{key:'item',name:'Item'},{key:'customer',name:'Customer'}];
var COM_DIM_NAME={product:'Product',main_group:'Main Group',state:'State',item:'Item',customer:'Customer'};
var comOrder=['product','main_group','state'];   // applied drill order
var comChosen=comOrder.slice();                  // working selection in the panel
function commoditySubGroups(){
  var out=[];
  for(var i=0;i<PRODUCTS.length;i++){if(PRODUCTS[i].u_type==='COMMODITY')out.push(PRODUCTS[i].u_sub_group);}
  return out;
}
// Total commodity product target (MonthlyTarget store, same as the commodity view).
function commodityTargetTotal(){
  var subs=commoditySubGroups(), t=0;
  for(var i=0;i<subs.length;i++)t+=Number(getDefTS('COMMODITY',subs[i]))||0;
  return t;
}
/* The litres-weighted target realise of the COMMODITY sub-group targets.
   Returns {sum, w} so a caller can fold it into the channel-target weighting and
   divide once. Weighting matches buildCardRows: a rate with no litres still counts
   once (wt=1) so it is not silently dropped.

   Why this exists: Target Ltr adds commodityTargetTotal() on top of the channel
   targets, but the target REALISE used to be weighted over the channel targets only.
   With no channel targets saved that left "no target realise" showing under a
   non-zero Target Ltr. */
function commodityTargetRealiseParts(){
  var subs=commoditySubGroups(), sum=0, w=0;
  for(var i=0;i<subs.length;i++){
    var tl=Number(getDefTS('COMMODITY',subs[i]))||0;
    var tr=Number(getDefTR('COMMODITY',subs[i]))||0;
    if(tr>0){ var wt=tl>0?tl:1; sum+=tr*wt; w+=wt; }
  }
  return {sum:sum, w:w};
}
function comDimValue(r,dim){
  if(dim==='product')return String(r.u_sub_group||'').trim().toUpperCase();
  if(dim==='main_group')return String(r.u_main_group||'').trim().toUpperCase()||'UNKNOWN';
  if(dim==='item')return String(r.item_name||'').trim().toUpperCase()||'—';
  if(dim==='customer')return String(r.card_name||'').trim().toUpperCase()||'—';
  return String(r.state||'UNKNOWN').trim().toUpperCase()||'UNKNOWN';
}
function commodityCleanRows(rows){
  var out=[];
  for(var i=0;i<rows.length;i++){
    var r=rows[i];
    if(String(r.u_type||'').toUpperCase()!=='COMMODITY')continue;
    if(!String(r.u_sub_group||'').trim())continue;
    out.push(r);
  }
  return out;
}
// Per-product totals (target + done) — independent of drill order, for KPIs/TOTAL.
function commodityProductTotals(rows){
  var clean=commodityCleanRows(rows), map={};
  for(var i=0;i<clean.length;i++){
    var sub=String(clean[i].u_sub_group||'').trim().toUpperCase();
    if(!map[sub])map[sub]={name:sub,done:0,lineTotal:0};
    map[sub].done+=Number(clean[i].liter)||0; map[sub].lineTotal+=Number(clean[i].line_total)||0;
  }
  commoditySubGroups().forEach(function(s){ if(!map[s])map[s]={name:s,done:0,lineTotal:0}; });
  return Object.keys(map).map(function(k){
    var c=map[k];
    c.target=Number(getDefTS('COMMODITY',k))||0;
    c.targetRealise=Number(getDefTR('COMMODITY',k))||0;
    return c;
  });
}
// Nested tree per the chosen drill order. Product nodes carry their target.
// Sales rows feed Done/Revenue; commodity OIH rows feed Order-in-Hand (both share the
// same dim fields so comDimValue buckets either kind).
function buildCommodityTree(rows,oihRows,lastRows,order){
  order=(order&&order.length)?order:['product'];
  var clean=commodityCleanRows(rows), oih=commodityCleanRows(oihRows||[]), last=commodityCleanRows(lastRows||[]);
  function group(salesSub,oihSub,lastSub,level){
    if(level>=order.length)return null;
    var dim=order[level], map={};
    function cell(v){if(!map[v])map[v]={name:v,dim:dim,done:0,lineTotal:0,oih:0,oihLineTotal:0,lastDone:0,_s:[],_o:[],_l:[]};return map[v];}
    for(var i=0;i<salesSub.length;i++){var r=salesSub[i],c=cell(comDimValue(r,dim));c.done+=Number(r.liter)||0;c.lineTotal+=Number(r.line_total)||0;c._s.push(r);}
    for(var j=0;j<oihSub.length;j++){var x=oihSub[j],c2=cell(comDimValue(x,dim));c2.oih+=Number(x.open_qty)||0;c2.oihLineTotal+=Number(x.open_value)||0;c2._o.push(x);}
    for(var m=0;m<lastSub.length;m++){var y=lastSub[m],c3=cell(comDimValue(y,dim));c3.lastDone+=Number(y.liter)||0;c3._l.push(y);}
    return Object.keys(map).map(function(k){
      var n=map[k]; n.kids=group(n._s,n._o,n._l,level+1); delete n._s; delete n._o; delete n._l;
      if(dim==='product'){ n.target=Number(getDefTS('COMMODITY',n.name))||0; n.targetRealise=Number(getDefTR('COMMODITY',n.name))||0; }
      return n;
    }).sort(function(a,b){return b.done-a.done||a.name.localeCompare(b.name);});
  }
  var tree=group(clean,oih,last,0)||[];
  // When Product is the top dimension, list every commodity product even at zero done.
  if(order[0]==='product'){
    var present={}; tree.forEach(function(n){present[n.name]=1;});
    commoditySubGroups().forEach(function(sub){
      if(!present[sub])tree.push({name:sub,dim:'product',done:0,lineTotal:0,oih:0,oihLineTotal:0,lastDone:0,kids:group([],[],[],1),
        target:Number(getDefTS('COMMODITY',sub))||0,targetRealise:Number(getDefTR('COMMODITY',sub))||0});
    });
    tree.sort(function(a,b){return b.done-a.done||a.name.localeCompare(b.name);});
  }
  return tree;
}
function comTotal(products){
  var t={name:'TOTAL',dim:'product',target:0,done:0,lineTotal:0,oih:0,oihLineTotal:0,trWsum:0,trW:0};
  for(var i=0;i<products.length;i++){var c=products[i];
    t.target+=c.target||0; t.done+=c.done||0; t.lineTotal+=c.lineTotal||0; t.oih+=c.oih||0; t.oihLineTotal+=c.oihLineTotal||0;
    if((c.targetRealise||0)>0&&(c.target||0)>0){t.trWsum+=c.targetRealise*c.target;t.trW+=c.target;}}
  t.targetRealise=t.trW>0?t.trWsum/t.trW:0;
  return t;
}
function comCells(node,isProduct,flexKey){
  // Effective target = the flexible override ONLY while Flex View is on (its Flex TGT column is
  // visible then); otherwise the product target, so Bal matches the shown "Tgt L". Saved flex
  // values persist in sc2FlexStore when Flex is off, so gate on sc2FlexOn() or Bal leaks them.
  var eff=(sc2FlexOn()&&flexKey&&sc2FlexStore.hasOwnProperty(flexKey))?sc2FlexStore[flexKey]:(node.target||0);
  var bal=isProduct?(eff-((node.done||0)+(node.oih||0))):null;
  var balClass=bal===null?'':(bal>=0?'sc2-bal-good':'sc2-bal-bad');
  var act=node.done>0?node.lineTotal/node.done:0;
  var doneCls='sc2-drillval com-done'+((node.done||0)!==0?' com-click':'');
  var oihCls='sc2-drillval com-oih'+((node.oih||0)>0?' com-click':'');
  // Bal Realise = (target revenue − actual revenue) / balance litres; product rows with a target only.
  var balRlz=(isProduct&&eff>0&&bal!==0)?((eff*(node.targetRealise||0))-(node.lineTotal||0))/bal:NaN;
  var balRlzStr=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';
  var oihRlz=(node.oih||0)>0?((node.oihLineTotal||0)/node.oih):NaN;   // ₹/L of open orders
  var oihRlzStr=isFinite(oihRlz)?'₹'+fNp(oihRlz,2):'&mdash;';
  return '<div class="sc2-drillval">'+(isProduct?fN(node.target||0):'&mdash;')+'</div>'
    +(sc2FlexOn()&&flexKey?sc2FlexCells(flexKey,node,!!isProduct):'')
    +'<div class="sc2-drillval">'+(isProduct&&(node.targetRealise||0)>0?fNp(node.targetRealise,2):'&mdash;')+'</div>'
    +'<div class="'+doneCls+'">'+fN(node.done||0)+'</div>'
    +'<div class="sc2-drillval">'+fN(node.lastDone||0)+'</div>'
    +'<div class="sc2-drillval">'+(node.done>0?fNp(act,2):'&mdash;')+'</div>'
    +'<div class="'+oihCls+'">'+fN(node.oih||0)+'</div>'
    +'<div class="sc2-drillval">'+oihRlzStr+'</div>'
    +'<div class="sc2-drillval sc2-bal-cell '+balClass+'">'+(bal===null?'&mdash;':fN(bal))+'</div>'
    +'<div class="sc2-drillval sc2-balrlz-cell">'+balRlzStr+'</div>';
}
// Map a commodity drill dim to the document-API filter key (main_group -> group).
var COM_TO_API={product:'product',main_group:'group',state:'state',item:'item',customer:'customer'};
var comNodeByPath={};
function comRow(node,level,path,gs,filt){
  filt=filt||{};
  var nodeFilt={};for(var fk in filt)nodeFilt[fk]=filt[fk];
  if(COM_TO_API[node.dim])nodeFilt[COM_TO_API[node.dim]]=node.name;
  comNodeByPath[path]={filters:nodeFilt,label:node.name};
  var hasKids=node.kids&&node.kids.length>0, isOpen=!!comExpanded[path];
  var indent=12+level*18, isProduct=node.dim==='product';
  var twirl=hasKids
    ? '<button class="sc2-com-twirl'+(isOpen?' open':'')+'" data-compath="'+esc(path)+'">'+(isOpen?'−':'+')+'</button>'
    : '<span class="sc2-com-twirl-empty"></span>';
  var html='<div class="sc2-drillrow sc2-wide com-l'+Math.min(level,2)+'" data-compath="'+esc(path)+'"'+gs+'>'
    +'<div class="sc2-drillname" style="padding-left:'+indent+'px">'+twirl+esc(node.name)+'</div>'
    +comCells(node,isProduct,path)+'</div>';
  if(hasKids&&isOpen){
    for(var i=0;i<node.kids.length;i++){var k=node.kids[i];html+=comRow(k,level+1,path+'>'+k.dim+':'+k.name,gs,nodeFilt);}
  }
  return html;
}
// Tree / total stashed for live footer refresh when a product's Flex TGT is edited.
var comTreeRef=null, comTotRef=null;
// Flex TGT total for the commodity footer. Flex applies to product nodes only, which can
// sit at any depth depending on drill order, so walk the whole tree and accumulate each
// overridden product's dent. Total flex = total target − Σ dents (matches the column).
function comFlexTotal(tree,totTarget){
  var dent=0;
  (function walk(nodes,prefix){
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i], path=prefix+n.dim+':'+n.name;
      if(n.dim==='product'&&sc2FlexStore.hasOwnProperty(path))
        dent+=(Number(n.target)||0)-(Number(sc2FlexStore[path])||0);
      if(n.kids&&n.kids.length)walk(n.kids,path+'>');
    }
  })(tree,'');
  return totTarget-dent;
}
// TOTAL-row cells for the commodity grid: like comCells but the Flex TGT is a read-only
// computed sum, with Dent / Bal / Bal Rlz tracking it. Column order mirrors comCells.
function comCellsTotal(tot,tree){
  var tgt=Number(tot.target)||0, flexSum=sc2FlexOn()?comFlexTotal(tree,tgt):tgt;
  var dent=tgt-flexSum, dCls=dent>0?'sc2-dent-pos':(dent<0?'sc2-dent-neg':'');
  var bal=flexSum-((tot.done||0)+(tot.oih||0)), balClass=bal>=0?'sc2-bal-good':'sc2-bal-bad';
  var act=tot.done>0?tot.lineTotal/tot.done:0;
  var balRlz=(flexSum>0&&bal!==0)?((flexSum*(tot.targetRealise||0))-(tot.lineTotal||0))/bal:NaN;
  var balRlzStr=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';
  var oihRlz=(tot.oih||0)>0?((tot.oihLineTotal||0)/tot.oih):NaN;
  var oihRlzStr=isFinite(oihRlz)?'₹'+fNp(oihRlz,2):'&mdash;';
  return '<div class="sc2-drillval">'+fN(tgt)+'</div>'
    +(sc2FlexOn()?'<div class="sc2-drillval sc2-flexcol sc2-flextot">'+fN(flexSum)+'</div>'
        +'<div class="sc2-drillval sc2-flexcol cdent '+dCls+'">'+(dent!==0?fN(dent):'&mdash;')+'</div>':'')
    +'<div class="sc2-drillval">'+((tot.targetRealise||0)>0?fNp(tot.targetRealise,2):'&mdash;')+'</div>'
    +'<div class="sc2-drillval com-done'+((tot.done||0)!==0?' com-click':'')+'">'+fN(tot.done||0)+'</div>'
    +'<div class="sc2-drillval">'+fN(tot.lastDone||0)+'</div>'
    +'<div class="sc2-drillval">'+(tot.done>0?fNp(act,2):'&mdash;')+'</div>'
    +'<div class="sc2-drillval com-oih'+((tot.oih||0)>0?' com-click':'')+'">'+fN(tot.oih||0)+'</div>'
    +'<div class="sc2-drillval">'+oihRlzStr+'</div>'
    +'<div class="sc2-drillval sc2-bal-cell '+balClass+'">'+fN(bal)+'</div>'
    +'<div class="sc2-drillval sc2-balrlz-cell">'+balRlzStr+'</div>';
}
// Refresh just the commodity footer's Flex/Dent/Bal/Bal-Rlz after a per-row edit.
function comRefreshTotal(){
  if(!comTotRef||!comTreeRef)return;
  var totRow=document.querySelector('#channelGrid .sc2-drillrow.sc2-state-total');
  if(!totRow)return;
  var tgt=Number(comTotRef.target)||0, flexSum=comFlexTotal(comTreeRef,tgt);
  var bal=flexSum-((comTotRef.done||0)+(comTotRef.oih||0));
  var ft=totRow.querySelector('.sc2-flextot'); if(ft)ft.innerHTML=fN(flexSum);
  var dc=totRow.querySelector('.cdent');
  if(dc){var d=tgt-flexSum;dc.innerHTML=(d!==0?fN(d):'&mdash;');dc.className='sc2-drillval sc2-flexcol cdent '+(d>0?'sc2-dent-pos':(d<0?'sc2-dent-neg':''));}
  var bc=totRow.querySelector('.sc2-bal-cell');
  if(bc){bc.innerHTML=fN(bal);bc.className='sc2-drillval sc2-bal-cell '+(bal>=0?'sc2-bal-good':'sc2-bal-bad');}
  var br=totRow.querySelector('.sc2-balrlz-cell');
  if(br){var rlz=(flexSum>0&&bal!==0)?((flexSum*(comTotRef.targetRealise||0))-(comTotRef.lineTotal||0))/bal:NaN;br.innerHTML=isFinite(rlz)?'₹'+fNp(rlz,2):'&mdash;';}
}
function renderCommodityTable(tree,tot){
  comTreeRef=tree; comTotRef=tot;
  var gs=' style="'+comGcols()+'"';
  var firstCol=comOrder.length?COM_DIM_NAME[comOrder[0]]:'Product';
  comNodeByPath={};
  comNodeByPath['__comtotal__']={filters:{},label:'TOTAL — Commodity'};
  var html='<div class="sc2-comhead">Sales Person &mdash; '+esc(COMMODITY_PERSON)+'</div>'
    +'<div class="sc2-drillcard sc2-drillfull sc2-comcard">'
    +'<div class="sc2-thead sc2-wide"'+gs+'>'
      +'<div class="sc2-tt-col" style="text-align:left">'+esc(comOrder.map(function(k){return COM_DIM_NAME[k];}).join(' › ')||firstCol)+'</div>'
      +'<div class="sc2-tt-col">Tgt L</div>'+sc2FlexHeadCols()+'<div class="sc2-tt-col">Tgt Rlz</div>'
      +'<div class="sc2-tt-col">Done L</div><div class="sc2-tt-col">Last Mo L</div><div class="sc2-tt-col">Done Rlz</div>'
      +'<div class="sc2-tt-col">OIH</div><div class="sc2-tt-col">OIH Rlz</div><div class="sc2-tt-col">Bal</div><div class="sc2-tt-col">Bal Rlz</div>'
    +'</div><div class="sc2-drillbody">';
  if(!tree.length){
    html+='<div class="sc2-state-empty">No commodity data in the current selection.</div>';
  }else{
    for(var i=0;i<tree.length;i++)html+=comRow(tree[i],0,tree[i].dim+':'+tree[i].name,gs,{});
    html+='<div class="sc2-drillrow sc2-wide sc2-state-total" data-compath="__comtotal__"'+gs+'><div class="sc2-drillname">TOTAL</div>'+comCellsTotal(tot,tree)+'</div>';
  }
  return html+'</div></div>';
}
function buildCommodityCsvRows(tree,tot){
  function balRlz(n){var b=(n.target||0)-((n.done||0)+(n.oih||0));return ((n.target||0)>0&&b!==0)?(((n.target||0)*(n.targetRealise||0))-(n.lineTotal||0))/b:null;}
  var out=[['Sales Person',COMMODITY_PERSON],
    [comOrder.map(function(k){return COM_DIM_NAME[k];}).join(' / '),'Target Ltr','Tgt Realise','Done Ltr','Last Mo Done Ltr','Done Realise','OIH Ltr','OIH Realise','Bal Ltr','Bal Realise']];
  function emit(node,level){
    var isProduct=node.dim==='product', act=node.done>0?node.lineTotal/node.done:0;
    var pad=level>0?new Array(level+1).join('  '):'';
    var br=isProduct?balRlz(node):null;
    out.push([pad+node.name,
      isProduct?csvInt(node.target):'', isProduct?csvDec(node.targetRealise):'',
      csvInt(node.done), csvInt(node.lastDone), node.done>0?csvDec(act):'', csvInt(node.oih),
      (node.oih>0?csvDec((node.oihLineTotal||0)/node.oih):''),
      isProduct?csvInt((node.target||0)-((node.done||0)+(node.oih||0))):'',
      (br!=null&&isFinite(br))?csvDec(br):'']);
    if(node.kids)node.kids.forEach(function(c){emit(c,level+1);});
  }
  for(var i=0;i<tree.length;i++)emit(tree[i],0);
  var tbr=balRlz(tot);
  out.push(['TOTAL',csvInt(tot.target),csvDec(tot.targetRealise),csvInt(tot.done),csvInt(tot.lastDone),
    tot.done>0?csvDec(tot.lineTotal/tot.done):'',csvInt(tot.oih),
    (tot.oih>0?csvDec((tot.oihLineTotal||0)/tot.oih):''),
    csvInt((tot.target||0)-((tot.done||0)+(tot.oih||0))),
    (tbr!=null&&isFinite(tbr))?csvDec(tbr):'']);
  return out;
}
// Commodity Order-in-Hand is a live snapshot (not date-bound), fetched once & cached.
var commodityOihRows=null, commodityOihLoading=false;
async function fetchCommodityOih(){
  if(commodityOihRows!==null)return commodityOihRows;
  try{var res=await fetch(API+'/api/commodity-oih-rows/?'+sc2DateQS(),{headers:{'X-CSRFToken':getCSRF()}});var p=res.ok?await res.json():{};commodityOihRows=p.data||[];}
  catch(e){commodityOihRows=[];}
  return commodityOihRows;
}
// "Last month" = the full previous calendar month relative to the slide-2 To-date.
// Fetched (and cached per range) via the same sales-data endpoint as the live rows.
var commodityLastRows=null, commodityLastLoading=false;
function prevMonthRange(toStr){
  var d=toStr?new Date(toStr+'T00:00:00'):new Date();
  if(isNaN(d.getTime()))d=new Date();
  var y=d.getFullYear(), m=d.getMonth()-1;   // 0-based month, step back one
  if(m<0){m=11;y--;}
  var start=new Date(y,m,1), end=new Date(y,m+1,0);
  function f(dt){return dt.getFullYear()+'-'+String(dt.getMonth()+1).padStart(2,'0')+'-'+String(dt.getDate()).padStart(2,'0');}
  return {start:f(start),end:f(end)};
}
async function fetchCommodityLastMonth(){
  if(commodityLastRows!==null)return commodityLastRows;
  var to=document.getElementById('sc2To')?document.getElementById('sc2To').value:'';
  var range=prevMonthRange(to);
  try{
    var res=await fetch(API+'/api/sales-data/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify({start_date:range.start,end_date:range.end})});
    var p=res.ok?await res.json():{};
    commodityLastRows=Array.isArray(p.channel_rows)?p.channel_rows:[];
  }catch(e){commodityLastRows=[];}
  return commodityLastRows;
}
function renderSlideTwoCommodity(){
  var grid=document.getElementById('channelGrid');
  if(!grid)return;
  var rows=getFilteredChannelRows();
  var oih=commodityOihRows||[];
  var last=commodityLastRows||[];
  var tot=comTotal(commodityProductTotals(rows));
  var co=commodityCleanRows(oih), totOih=0; for(var i=0;i<co.length;i++)totOih+=Number(co[i].open_qty)||0;
  tot.oih=totOih;
  var cl=commodityCleanRows(last), totLast=0; for(var li=0;li<cl.length;li++)totLast+=Number(cl[li].liter)||0;
  tot.lastDone=totLast;
  var tree=buildCommodityTree(rows,oih,last,comOrder);
  // Bal Ltr KPI = the TOTAL row's "Bal" = flex-adjusted target − Done − OIH (matches comCellsTotal).
  var comFlex=sc2FlexOn()?comFlexTotal(tree,tot.target||0):(tot.target||0);
  setSlideTwoKpis(tot.target,tot.done,tot.targetRealise,tot.done>0?tot.lineTotal/tot.done:0,tot.oih,
                comFlex-((tot.done||0)+(tot.oih||0)),
                (tot.oih||0)>0?((tot.oihLineTotal||0)/tot.oih):0);
  sc2CsvRows=buildCommodityCsvRows(tree,tot);
  grid.innerHTML=renderCommodityTable(tree,tot);
  // First paint has no OIH yet — fetch the live snapshot, then repaint.
  if(commodityOihRows===null&&!commodityOihLoading){
    commodityOihLoading=true;
    fetchCommodityOih().then(function(){commodityOihLoading=false;if(sc2Seg()==='COMMODITY')renderSlideTwoCommodity();});
  }
  // Likewise fetch last month's done once, then repaint.
  if(commodityLastRows===null&&!commodityLastLoading){
    commodityLastLoading=true;
    fetchCommodityLastMonth().then(function(){commodityLastLoading=false;if(sc2Seg()==='COMMODITY')renderSlideTwoCommodity();});
  }
}
// Show/hide the channel Drill-By controls vs. the commodity drill panel by segment.
function applySlideTwoSegmentUI(seg){
  var isCom=seg==='COMMODITY';
  var db=document.getElementById('sc2DrillBy'), dbl=document.querySelector('label[for="sc2DrillBy"]');
  if(db)db.style.display=isCom?'none':'';
  if(dbl)dbl.style.display=isCom?'none':'';
  var dyn=document.getElementById('sc2DynWrap');
  var showDyn=!isCom&&getSlideTwoDrillBy()!=='main_group';
  if(dyn)dyn.style.display=showDyn?'':'none';
  var cw=document.getElementById('comDrillWrap');
  if(cw)cw.style.display=isCom?'':'none';
}
/* ----- Commodity drill-order panel (Product / Main Group / State) ----- */
function comUpdateDrillLabel(){
  var el=document.getElementById('comDrillLabel');
  if(el)el.textContent=comOrder.length?comOrder.map(function(k){return COM_DIM_NAME[k];}).join(' › '):'— none —';
}
function comRenderDpList(){
  var list=document.getElementById('comDpList'); if(!list)return;
  list.innerHTML='';
  COM_DIMS.forEach(function(d){
    var pos=comChosen.indexOf(d.key);
    var item=document.createElement('label');
    item.className='com-dp-item'+(pos!==-1?' checked':'');
    item.innerHTML='<input type="checkbox" '+(pos!==-1?'checked':'')+'>'
      +'<span class="com-dp-name">'+d.name+'</span>'
      +'<span class="com-pos">'+(pos!==-1?pos+1:'')+'</span>';
    item.querySelector('input').addEventListener('change',function(){
      var i=comChosen.indexOf(d.key);
      if(this.checked){ if(i===-1)comChosen.push(d.key); }
      else if(i!==-1)comChosen.splice(i,1);
      comRenderDpList();
    });
    list.appendChild(item);
  });
}
function comInitDrillPanel(){
  var btn=document.getElementById('comDrillBtn'), panel=document.getElementById('comDrillPanel');
  if(!btn||!panel)return;
  comUpdateDrillLabel();
  btn.addEventListener('click',function(e){ e.stopPropagation(); comChosen=comOrder.slice(); comRenderDpList(); panel.classList.toggle('open'); });
  panel.addEventListener('click',function(e){ e.stopPropagation(); });
  document.addEventListener('click',function(){ panel.classList.remove('open'); });
  document.getElementById('comSelAll').addEventListener('click',function(){ comChosen=COM_DIMS.map(function(d){return d.key;}); comRenderDpList(); });
  document.getElementById('comClrAll').addEventListener('click',function(){ comChosen=[]; comRenderDpList(); });
  document.getElementById('comApply').addEventListener('click',function(){
    if(!comChosen.length){ showToast('Pick at least one drill dimension','info'); return; }
    comOrder=comChosen.slice(); comExpanded={};
    comUpdateDrillLabel(); panel.classList.remove('open');
    renderSlideTwoCommodity();
  });
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',comInitDrillPanel);
else comInitDrillPanel();
// Expand/collapse commodity tree rows (delegated; survives re-renders).
document.addEventListener('click',function(e){
  var b=e.target&&e.target.closest?e.target.closest('.sc2-com-twirl'):null;
  if(!b)return;
  e.stopPropagation();
  var p=b.getAttribute('data-compath');
  comExpanded[p]=!comExpanded[p];
  renderSlideTwoCommodity();
});
// Click a commodity Done / OIH value -> list the underlying invoices / open SOs.
document.addEventListener('click',function(e){
  if(!e.target||!e.target.closest)return;
  var cell=e.target.closest('#channelGrid .sc2-drillval.com-click');
  if(!cell)return;
  var row=cell.closest('.sc2-drillrow'), path=row?row.getAttribute('data-compath'):'';
  var node=path?comNodeByPath[path]:null;
  if(!node)return;
  var metric=cell.classList.contains('com-oih')?'oih':'done';
  openChannelDocs(metric,node.filters,node.label,{channel:'COMMODITY',seg:'COMMODITY'});
});
function buildStateRows(rows,members,groupByMainGroup,channelName){
  var whitelist=channelName?CHANNEL_STATE_WHITELIST[channelName]:null;
  var map={};
  var totalDone=0;
  // Seed fixed rows so whitelisted states always appear (even with zero sales).
  if(whitelist){for(var w=0;w<whitelist.length;w++)map[whitelist[w].label]={name:whitelist[w].label,done:0};}
  for(var i=0;i<rows.length;i++){
    var row=rows[i];
    if(members.indexOf(row.u_main_group)===-1)continue;
    var stateName;
    if(whitelist){
      stateName=channelStateLabel(channelName,row.state);
      if(stateName===null)continue; // state not in this channel's fixed list
    }else{
      stateName=groupByMainGroup?(row.u_main_group||'UNKNOWN'):(row.state||'UNKNOWN');
    }
    if(!map[stateName])map[stateName]={name:stateName,done:0};
    var doneValue=Number(row.liter)||0;
    map[stateName].done+=doneValue;
    totalDone+=doneValue;
  }
  var list=Object.keys(map).map(function(key){return map[key];});
  if(whitelist){
    // Preserve the sheet's order for fixed rows.
    var order={};for(var oi=0;oi<whitelist.length;oi++)order[whitelist[oi].label]=oi;
    list.sort(function(a,b){return (order[a.name]||0)-(order[b.name]||0);});
  }else
  list.sort(function(a,b){return b.done-a.done||a.name.localeCompare(b.name);});
  for(var si=0;si<list.length;si++){
    list[si].share=totalDone>0?(list[si].done/totalDone)*100:0;
  }
  return list;
}
function getStatusInfo(pct){
  if(pct >= 100)return {label:'Achieved', cls:'good'};
  if(pct >= 85)return {label:'Near Target', cls:'warn'};
  return {label:'Missed', cls:'bad'};
}
function getChannelTitle(name){
  var titles={
    GT:'General Trade (GT) Performance',
    ROI:'Rest Of India (ROI) Tracking',
    MT:'Modern Trade (MT) Market Trends',
    ECOM:'E-Commerce Performance',
    HORECA:'Horeca Channel Performance',
    CSD:'CSD Channel Performance',
    REST:'Rest Segment Tracking'
  };
  return titles[name]||name;
}
function estimateStateTargets(states,totalTarget,totalDone){
  for(var i=0;i<states.length;i++){
    states[i].target=totalDone>0?(totalTarget*(states[i].done/totalDone)):0;
    states[i].progress=states[i].target>0?(states[i].done/states[i].target)*100:0;
    states[i].balance=states[i].target-states[i].done;
  }
  return states;
}
function renderStateBreakdown(states){
  if(!states.length)return '<div class="sc2-state-wrap"><div class="sc2-state-empty">No state data in the current selection.</div></div>';
  var html='<div class="sc2-state-wrap"><div class="sc2-state-head"><div>Area</div><div class="sc2-state-val">Target Value</div><div class="sc2-state-val">Done Value</div><div class="sc2-state-bar">Balance Value</div><div class="sc2-status">Status</div></div>';
  for(var i=0;i<states.length;i++){
    var st=states[i];
    var status=getStatusInfo(st.progress);
    var fill=Math.max(8,Math.min(st.progress,100));
    html+='<div class="sc2-state-row"><div class="sc2-state-name">'+esc(st.name)+'</div><div class="sc2-state-val">T'+fN(st.target)+'</div><div class="sc2-state-val">D'+fN(st.done)+'</div><div class="sc2-state-bar"><span><b style="width:'+fill+'%"></b></span></div><div class="sc2-status"><span class="sc2-status-pill '+status.cls+'">'+status.label+'</span></div></div>';
  }
  html+='</div>';
  return html;
}
var CHANNEL_COLORS={
  GT:{band:'#b8e8fb',ring:'#51b7d8'},
  ROI:{band:'#ffb7a7',ring:'#f26a4b'},
  MT:{band:'#ffd59b',ring:'#f59e0b'},
  ECOM:{band:'#e9d5ff',ring:'#9333ea'},
  HORECA:{band:'#c8f1db',ring:'#16a34a'},
  CSD:{band:'#cdeefe',ring:'#0ea5e9'},
  REST:{band:'#fbe4a2',ring:'#d97706'}
};
function channelCardHtml(item){
  var balClass=item.balance>=0?'sc2-bal-good':'sc2-bal-bad';
  var pct=Math.max(0,Math.min(item.progress,100));
  var accent=CHANNEL_COLORS[item.name]||{band:'#dbeafe',ring:'#60a5fa'};
  var channels=Array.isArray(item.members)?item.members.join(' · '):'';
  return '<a class="sc2-card" style="--accent:'+accent+'" href="/realise/channel/'+encodeURIComponent(item.name)+'/">'
    +'<div class="sc2-head"><div class="sc2-h-left"><h3>'+esc(item.name)+'</h3>'
      +(channels?'<div class="sc2-members">'+esc(channels)+'</div>':'')
    +'</div><div class="sc2-h-tag">Channel</div></div>'
    +'<div class="sc2-body">'
    +'<div class="sc2-metrics">'
    +'<div class="sc2-metric"><label>Target</label><div>'+fN(item.target)+'</div></div>'
    +'<div class="sc2-metric"><label>Done</label><div>'+fN(item.done)+'</div></div>'
    +'<div class="sc2-metric"><label>Bal</label><div class="'+balClass+'">'+fN(item.balance)+'</div></div>'
    +'<div class="sc2-ring" style="--p:'+pct.toFixed(0)+'"><span>'+fN(pct,0)+'%</span></div>'
    +'</div>'
    +renderStateBreakdown(item.states||[])
    +'</div></a>';
}
function themedStatusInfo(pct){
  if(pct >= 100)return {label:'Achieved', cls:'good'};
  if(pct >= 85)return {label:'Near Target', cls:'warn'};
  return {label:'Missed', cls:'bad'};
}
function themedChannelTitle(name){
  var titles={
    GT:'General Trade (GT) Performance',
    ROI:'Rest Of India (ROI) Tracking',
    MT:'Modern Trade (MT) Market Trends',
    ECOM:'E-Commerce Performance',
    HORECA:'Horeca Channel Performance',
    CSD:'CSD Channel Performance',
    REST:'Rest Segment Tracking'
  };
  return titles[name]||name;
}
function themedStateRows(rows,totalTarget,totalDone){
  for(var i=0;i<rows.length;i++){
    rows[i].target=totalDone>0?(totalTarget*(rows[i].done/totalDone)):0;
    rows[i].progress=rows[i].target>0?(rows[i].done/rows[i].target)*100:0;
  }
  return rows;
}
function renderThemedStateBreakdown(states){
  if(!states.length)return '<div class="sc2-state-wrap"><div class="sc2-state-empty">No state data in the current selection.</div></div>';
  var html='<div class="sc2-state-wrap"><div class="sc2-state-head"><div>Area</div><div class="sc2-state-val">Target Value</div><div class="sc2-state-val">Done Value</div><div class="sc2-state-bar">Balance Value</div><div class="sc2-status">Status</div></div>';
  for(var i=0;i<states.length;i++){
    var st=states[i];
    var status=themedStatusInfo(st.progress);
    var fill=Math.max(8,Math.min(st.progress,100));
    var balance=st.target-st.done;
    html+='<div class="sc2-state-row"><div class="sc2-state-name">'+esc(st.name)+'</div><div class="sc2-state-val">T'+fN(st.target)+'</div><div class="sc2-state-val">D'+fN(st.done)+'</div><div class="sc2-state-bar"><span><b style="width:'+fill+'%"></b></span></div><div class="sc2-status"><span class="sc2-status-pill '+status.cls+'">'+status.label+'</span></div></div>';
  }
  html+='</div>';
  return html;
}
function cardCells(c,isTotal){
  var bal=(c.target||0)-((c.done||0)+(c.oih||0)), balClass=bal>=0?'sc2-bal-good':'sc2-bal-bad';
  var balWo=(c.target||0)-(c.done||0), balWoClass=balWo>=0?'sc2-bal-good':'sc2-bal-bad';   // Target − Done (no OIH)
  // Bal Realise = (target revenue − actual revenue) / balance litres = the ₹/L still
  // needed on the remaining balance to hit the target revenue.
  // Only meaningful when a target exists (no target ⇒ no target revenue to balance against).
  var balRlz=((c.target||0)>0&&bal!==0)?(((c.target||0)*(c.targetRealise||0))-(c.lineTotal||0))/bal:NaN;
  var balRlzStr=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';
  var oihRlz=(c.oih||0)>0?((c.oihLineTotal||0)/c.oih):NaN;   // ₹/L of open orders
  var oihRlzStr=isFinite(oihRlz)?'₹'+fNp(oihRlz,2):'&mdash;';
  // Tgt Realise comes off the target row; Done Realise is revenue / done litres.
  var tgtRlz=Number(c.targetRealise)||0;
  var tgtRlzStr=tgtRlz>0?'₹'+fNp(tgtRlz,2):'&mdash;';
  var actRlz=(c.done||0)>0?((c.lineTotal||0)/c.done):NaN;
  var actRlzStr=(isFinite(actRlz)&&actRlz>0)?'₹'+fNp(actRlz,2):'&mdash;';
  void balWoClass;   // still computed above; shown in the detail drill, not here
  return '<div class="sc2-state-name">'+esc(isTotal?'TOTAL':c.name)+'</div>'
    +'<div class="sc2-state-val">'+fN(c.target||0)+'</div>'
    +'<div class="sc2-state-val">'+tgtRlzStr+'</div>'
    +'<div class="sc2-state-val">'+fN(c.done||0)+'</div>'
    +'<div class="sc2-state-val">'+actRlzStr+'</div>'
    +'<div class="sc2-state-val">'+fN(c.oih||0)+'</div>'
    +'<div class="sc2-state-val">'+oihRlzStr+'</div>'
    +'<div class="sc2-state-val '+balClass+'">'+fN(bal)+'</div>'
    +'<div class="sc2-state-val">'+balRlzStr+'</div>';
}
function themedChannelCardHtml(item){
  var accent=CHANNEL_COLORS[item.name]||{band:'#dbeafe',ring:'#60a5fa'};
  // In "All", hide zero-activity rows so cards aren't padded with empty states; when a
  // specific segment (Premium/Commodity) is selected, show every row even at zero so
  // gaps in that segment are visible.
  var showAll=!!sc2Seg();
  var data=(item.rowsData||[]).filter(function(c){return showAll||(c.done||0)>0||(c.oih||0)>0||(c.target||0)>0;});
  var cardTitle=item.title||themedChannelTitle(item.name)||item.name;
  var firstColLabel=item.firstColLabel||'State/Area';
  var rows='';
  if(!data.length){
    rows='<div class="sc2-state-empty">No area data in the current selection.</div>';
  }else{
    for(var i=0;i<data.length;i++)rows+='<div class="sc2-state-row sc2-oih">'+cardCells(data[i],false)+'</div>';
  }
  var totRow='<div class="sc2-state-row sc2-state-total sc2-oih">'+cardCells(item,true)+'</div>';
  var shortCode=item.name.length>3?item.name.slice(0,2):item.name;
  item.title=cardTitle;
  item.members=(item.members&&item.members.length)?item.members:[item.name];
  return '<div class="sc2-card '+(item.name==='REST'?'sc2-card-rest':'sc2-card-main')+'" style="--accent:'+accent.band+';--accent-strong:'+accent.ring+';cursor:pointer" onclick="openChannelDetail('+"'"+esc(item.name)+"'"+')">'
    +'<div class="sc2-titlebar">'
      +'<div class="sc2-icon">'+esc(shortCode)+'</div>'
      // No "GT - " style prefix: the round badge to the left already shows the code.
      +'<div class="sc2-titletext"><strong>'+esc(item.title.replace(/ Performance| Tracking| Market Trends/g,''))+'</strong><span>'+esc(item.members.join(' · '))+'</span></div>'
    +'</div>'
    /* Column order: every litres figure is followed by its own realise, so they read
       in pairs - Target/Tgt, Done/Act, OIH/OIH, Bal/Bal. "Bal w/o OIH" was dropped
       from this table; it is still available in the channel-detail drill. */
    +'<div class="sc2-thead sc2-oih">'
      +'<div class="sc2-tt-col" style="text-align:left">'+esc(firstColLabel)+'</div>'
      +'<div class="sc2-tt-col">Target L</div>'
      +'<div class="sc2-tt-col">Tgt Realise</div>'
      +'<div class="sc2-tt-col">Done L</div>'
      +'<div class="sc2-tt-col">Done Realise</div>'
      +'<div class="sc2-tt-col">Order in Hand</div>'
      +'<div class="sc2-tt-col">OIH Realise</div>'
      +'<div class="sc2-tt-col">Bal</div>'
      +'<div class="sc2-tt-col">Bal Realise</div>'
    +'</div>'
    +'<div class="sc2-state-scroll">'+rows+'</div>'
    +(data.length?totRow:'')
    +'</div>';
}
// ===== Channel detail modal (Target / Done / Order in Hand / Bal) =====
var detailChannel=null, detailTargetNodes=[], detailOihRows=[], detailSeg='';
var detailTargetCache={}, detailOihRowsCache=null;
function channelMembers(name){
  for(var i=0;i<CHANNEL_BLOCKS.length;i++)if(CHANNEL_BLOCKS[i].name===name)return CHANNEL_BLOCKS[i].members;
  return [name];
}
function sc2Seg(){var e=document.getElementById('sc2Segment');return e?e.value:'';}
async function fetchTargetNodes(month,year,segOverride){
  var seg=(segOverride!==undefined&&segOverride!==null)?segOverride:sc2Seg(), key=year+'-'+month+'-'+seg;
  if(detailTargetCache[key])return detailTargetCache[key];
  try{var res=await fetch(API+'/api/target-nodes/?month='+month+'&year='+year+'&seg='+encodeURIComponent(seg),{headers:{'X-CSRFToken':getCSRF()}});var p=res.ok?await res.json():{};detailTargetCache[key]=p.data||[];}
  catch(e){detailTargetCache[key]=[];}
  return detailTargetCache[key];
}
// Slide-2 date range as a query string — used to build the customer->state map so
// Order-in-Hand state matches the Done/sales side.
function sc2DateQS(){
  var f=document.getElementById('sc2From'),t=document.getElementById('sc2To'),qs=[];
  if(f&&f.value)qs.push('start='+encodeURIComponent(f.value));
  if(t&&t.value)qs.push('end='+encodeURIComponent(t.value));
  return qs.join('&');
}
async function fetchOrderInHandRows(){
  if(detailOihRowsCache)return detailOihRowsCache;
  try{var res=await fetch(API+'/api/order-in-hand-rows/?'+sc2DateQS(),{headers:{'X-CSRFToken':getCSRF()}});var p=res.ok?await res.json():{};detailOihRowsCache=p.data||[];}
  catch(e){detailOihRowsCache=[];}
  return detailOihRowsCache;
}
async function openChannelDetail(name){
  // Month-wise comparison moved to the standalone "Compare Sales" tab; slide-2 is
  // date-range only now.
  detailChannel=name;
  var overlay=document.getElementById('channelDetailModal');
  var header=document.getElementById('cdHeaderEl');
  var accent=(typeof CHANNEL_COLORS!=='undefined'&&CHANNEL_COLORS[name])||{band:'#2563eb',ring:'#1d4ed8'};
  header.style.setProperty('--accent',accent.band);
  header.style.setProperty('--accent-strong',accent.ring);
  var t=(typeof themedChannelTitle==='function'&&themedChannelTitle(name))||name;
  document.getElementById('cdTitle').textContent=name+' — '+String(t).replace(/ Performance| Tracking| Market Trends/g,'');
  // Sensible default per card: group-based REST starts at Main Group, others at State.
  detailOrder=(name==='REST')?['group']:['state'];
  detailChosen=detailOrder.slice();
  cdRefine.reset();
  cdExpanded={};
  // Flex TGT entries are scratch — cleared per channel. The Fixed/Flexible choice lives
  // on the toolbar (beside Today's Sales) and is picked before opening the card.
  cdFlex={};
  var cdViewSel=document.getElementById('cdView');
  cdViewMode=(cdViewSel&&cdViewSel.value==='flex')?'flex':'fixed';
  var cdTbl=document.querySelector('#channelDetailModal .cd-table');
  if(cdTbl)cdTbl.classList.toggle('cd-flex-on',cdViewMode==='flex');
  // Seed the modal's own Premium/Commodity selector from the slide-2 toolbar segment.
  detailSeg=sc2Seg();
  var cdType=document.getElementById('cdType');if(cdType)cdType.value=detailSeg;
  updateCdDrillLabel();renderCdDpList();
  overlay.classList.add('show');
  renderChannelDetail();                          // instant: Done comes from client data
  var period=getResolvedChannelPeriod();
  try{
    var res=await Promise.all([fetchTargetNodes(period.month,period.year,detailSeg),fetchOrderInHandRows()]);
    detailTargetNodes=res[0]||[];detailOihRows=res[1]||[];
  }catch(e){detailTargetNodes=detailTargetNodes||[];detailOihRows=detailOihRows||[];}
  if(overlay.classList.contains('show'))renderChannelDetail();  // fill Target & Order in Hand
}
function closeChannelDetail(){document.getElementById('channelDetailModal').classList.remove('show');}
// Modal-local Premium/Commodity selector: re-filter Done/Revenue instantly, then
// re-fetch the segment's targets (Order-in-Hand has no segment, so it stays put).
async function onCdTypeChange(){
  var sel=document.getElementById('cdType');
  detailSeg=sel?sel.value:'';
  renderChannelDetail();                          // instant: Done from client rows
  var period=getResolvedChannelPeriod();
  try{ detailTargetNodes=await fetchTargetNodes(period.month,period.year,detailSeg)||[]; }
  catch(e){ detailTargetNodes=detailTargetNodes||[]; }
  if(document.getElementById('channelDetailModal').classList.contains('show'))renderChannelDetail();
}
// Fixed View (default) vs Flexible View. Flexible reveals the Flex TGT / Dent columns
// (pure CSS toggle on the table); we re-render so the empty-state colspan stays correct.
function onCdViewChange(){
  var sel=document.getElementById('cdView');
  cdViewMode=(sel&&sel.value==='flex')?'flex':'fixed';
  var tbl=document.querySelector('#channelDetailModal .cd-table');
  if(tbl)tbl.classList.toggle('cd-flex-on',cdViewMode==='flex');
  // Toolbar control: the card may be closed when this changes — only re-render if open.
  if(document.getElementById('channelDetailModal').classList.contains('show'))renderChannelDetail();
  // Re-render the live slide-2 table so its Flex TGT / Dent columns appear or hide.
  if(typeof sc2Fetched!=='undefined'&&sc2Fetched){
    if(sc2Seg()==='COMMODITY')renderSlideTwoCommodity();
    else if(getSlideTwoDrillBy()!=='main_group')sc2DynRerender();
  }
}
function detailStateLabel(channel,stateName){
  if(CHANNEL_STATE_WHITELIST[channel])return channelStateLabel(channel,stateName);
  return String(stateName||'UNKNOWN').trim().toUpperCase()||'UNKNOWN';
}
// Customer (CardName) only exists on the sales rows; targets have no customer, so
// they bucket under '—'. OIH rows do carry customer/product/item breakdowns, so the
// finer drills can now split them the same way the slide-1 dashboard does.
var CD_NO_CUSTOMER='—';
// Targets carry no product/item/customer breakdown, so they bucket under '—' for
// those finer dims (same convention as Customer). OIH rows do have product/item.
var CD_NONE='—';
// A channel-total target (REST/ECOM: main_group=<channel>, state='') has no state to
// bucket under. Show it as this explicit row so it's visible AND counted in TOTAL.
var CD_WHOLE='WHOLE CHANNEL';
// Slide-2 channel rows filtered by the modal-local Premium/Commodity selector.
function modalChannelRows(){
  var rows=[];
  for(var i=0;i<sc2Rows.length;i++){
    if(detailSeg&&sc2Rows[i].u_type!==detailSeg)continue;
    rows.push(sc2Rows[i]);
  }
  return rows;
}
function buildChannelDetailLeaves(){
  var channel=detailChannel, members=channelMembers(channel), map={};
  function cell(group,state,person,customer,product,item,sku){var k=group+'||'+state+'||'+person+'||'+customer+'||'+product+'||'+item+'||'+sku;if(!map[k])map[k]={group:group,state:state,person:person,customer:customer,product:product,item:item,sku:sku,target:0,done:0,oih:0,lineTotal:0,oihLineTotal:0,targetRealise:0};return map[k];}
  var rows=modalChannelRows();
  for(var i=0;i<rows.length;i++){
    var r=rows[i],g=String(r.u_main_group||'').toUpperCase();if(members.indexOf(g)===-1)continue;
    var st=detailStateLabel(channel,r.state);if(st===null)continue;
    var cust=String(r.card_name||'').trim().toUpperCase()||CD_NO_CUSTOMER;
    var prod=String(r.u_sub_group||'').trim().toUpperCase()||CD_NONE;
    var item=String(r.item_name||'').trim().toUpperCase()||CD_NONE;
    var c=cell(g,st,assignedPerson(g,r.state)||'—',cust,prod,item,String(r.sku||'').trim().toUpperCase()||CD_NONE); c.done+=Number(r.liter)||0; c.lineTotal+=Number(r.line_total)||0;
  }
  for(var t=0;t<detailTargetNodes.length;t++){
    var n=detailTargetNodes[t],g2=String(n.main_group||'').toUpperCase();
    var isChanLevel=(g2===String(channel||'').toUpperCase());   // REST/ECOM channel-total targets carry main_group=<channel>
    if(!isChanLevel && members.indexOf(g2)===-1)continue;
    var st2=(isChanLevel && !String(n.state||'').trim())?CD_WHOLE:detailStateLabel(channel,n.state);
    if(st2===null)continue;
    var p2=String(n.sales_person||'').toUpperCase()||assignedPerson(g2,n.state)||channelDefaultPerson(channel)||'—';
    var c2=cell(g2,st2,p2,CD_NO_CUSTOMER,CD_NONE,CD_NONE,CD_NONE); c2.target+=Number(n.target_ltrs)||0; c2.targetRealise=Number(n.target_realise)||c2.targetRealise;
  }
  for(var o=0;o<detailOihRows.length;o++){
    var x=detailOihRows[o],g3=String(x.main_group||'').toUpperCase();if(members.indexOf(g3)===-1)continue;
    if(detailSeg&&String(x.u_type||'').toUpperCase()!==detailSeg)continue;   // honour Premium/Commodity
    var st3=detailStateLabel(channel,x.state);if(st3===null)continue;
    var p3=String(x.sales_person||'').toUpperCase()||assignedPerson(g3,x.state)||'—';
    var cust3=String(x.card_name||'').trim().toUpperCase()||CD_NO_CUSTOMER;
    var prod3=String(x.u_sub_group||'').trim().toUpperCase()||CD_NONE;
    var item3=String(x.item_name||'').trim().toUpperCase()||CD_NONE;
    var oc=cell(g3,st3,p3,cust3,prod3,item3,String(x.sku||'').trim().toUpperCase()||CD_NONE); oc.oih+=Number(x.open_qty)||0; oc.oihLineTotal+=Number(x.open_value)||0;
  }
  return Object.keys(map).map(function(k){return map[k];});
}
function dimMeta(dim){
  return ({group:{label:'Main Group',tag:'GROUP',cls:'group'},
           state:{label:'State',tag:'STATE',cls:'state'},
           person:{label:'Person',tag:'PERSON',cls:'person'},
           product:{label:'Product',tag:'PRODUCT',cls:'group'},
           item:{label:'Item Name',tag:'ITEM',cls:'item'},
           customer:{label:'Customer',tag:'CUSTOMER',cls:'person'}})[dim]||{label:dim,tag:'',cls:''};
}
function orderStates(channel,names){
  var wl=CHANNEL_STATE_WHITELIST[channel];
  if(wl){var ord={};for(var i=0;i<wl.length;i++)ord[wl[i].label]=i;return names.sort(function(a,b){return (ord[a]==null?99:ord[a])-(ord[b]==null?99:ord[b]);});}
  return names.sort();
}
function orderPersons(names){
  var ord={};for(var i=0;i<ASSIGNED_PERSONS.length;i++)ord[ASSIGNED_PERSONS[i]]=i;
  return names.sort(function(a,b){return (ord[a]==null?99:ord[a])-(ord[b]==null?99:ord[b])||a.localeCompare(b);});
}
function orderValues(dim,m){
  var names=Object.keys(m);
  if(dim==='person')return orderPersons(names);
  if(dim==='state')return orderStates(detailChannel,names);
  return names.sort(function(a,b){return aggList(m[b]).done-aggList(m[a]).done||a.localeCompare(b);}); // group: by done desc
}
function aggList(list){
  var t=0,d=0,o=0,lt=0,olt=0,trws=0,trw=0,trS=0,trN=0;
  for(var i=0;i<list.length;i++){var x=list[i];t+=x.target;d+=x.done;o+=x.oih;lt+=(x.lineTotal||0);olt+=(x.oihLineTotal||0);
    if((x.targetRealise||0)>0){trN++;trS+=x.targetRealise;var wt=x.target>0?x.target:0;trws+=x.targetRealise*wt;trw+=wt;}}
  return {target:t,done:d,oih:o,lineTotal:lt,oihLineTotal:olt,
    targetRealise:trw>0?trws/trw:(trN>0?trS/trN:0),
    actualRealise:d>0?lt/d:0};
}
function groupLeavesBy(leaves,dim){var m={};leaves.forEach(function(l){var k=l[dim];(m[k]=m[k]||[]).push(l);});return m;}
// Collapsible tree: parent rows carry a twirl and toggle their children.
var cdExpanded={}, cdTree=[], cdNodeByPath={}, cdTotalAgg=null;
// Flexible View: 'fixed' (default) or 'flex'. cdFlex maps a row's drill path to a
// temporary user-entered target (litres); Dent = Target L − Flex TGT. Scratch only —
// reset every time the modal opens, never sent to the server.
var cdViewMode='fixed', cdFlex={};
function buildDetailTree(leaves,dims,depth,prefix,filt){
  filt=filt||{};
  var d=dims[depth],m=groupLeavesBy(leaves,d),nodes=[];
  orderValues(d,m).forEach(function(v){
    // Targets have no product/item breakdown, so they land in the '—' bucket.
    // Hide that empty bucket in product/item drills; TOTAL still counts them.
    if((d==='product'||d==='item')&&v===CD_NONE)return;
    var path=prefix+''+v;
    var nodeFilt={};for(var fk in filt)nodeFilt[fk]=filt[fk];nodeFilt[d]=v;
    var node={path:path,dim:d,label:v,filters:nodeFilt,agg:aggList(m[v]),leaf:depth+1>=dims.length,children:[]};
    cdNodeByPath[path]=node;
    if(!node.leaf)node.children=buildDetailTree(m[v],dims,depth+1,path,nodeFilt);
    nodes.push(node);
  });
  return nodes;
}
function cdBal(a){return a.target-(a.done+a.oih);}  // Bal = Target − (Done + Order in Hand)
// Flexible View columns for one row: an editable Flex TGT input (pre-filled from the
// temporary cdFlex store) and the computed Dent (Target L − Flex TGT). Always emitted;
// CSS hides them unless the table carries .cd-flex-on (View = Flexible).
function cdFlexCells(path,a){
  var tgt=Number(a.target)||0, has=cdFlex.hasOwnProperty(path), fv=has?cdFlex[path]:null;
  var dent=has?(tgt-fv):NaN;
  var dCls=isFinite(dent)?(dent>0?'cd-dent-pos':(dent<0?'cd-dent-neg':'')):'';
  // data-* carry the row's Done/OIH/rate/revenue so the input handler can recompute
  // Bal (and Bal Realise) live on the flexible target without a full re-render.
  return '<td class="cd-flexcol cd-flexin">'
      +'<input type="number" class="cd-flex-input" step="any" inputmode="decimal"'
      +' data-tgt="'+tgt+'" data-done="'+(Number(a.done)||0)+'" data-oih="'+(Number(a.oih)||0)+'"'
      +' data-tr="'+(Number(a.targetRealise)||0)+'" data-lt="'+(Number(a.lineTotal)||0)+'"'
      +' placeholder="'+fN(tgt)+'" value="'+(has?String(fv):'')+'"></td>'
    +'<td class="cd-flexcol cd-dent '+dCls+'">'+(isFinite(dent)?fN(dent):'&mdash;')+'</td>';
}
function cdMetricCells(a,path){
  // Effective target = the flexible override (if one is entered for this row) else the
  // real target. Bal and Bal Realise are computed on it so they track Flex TGT.
  var eff=cdFlex.hasOwnProperty(path)?cdFlex[path]:(a.target||0);
  var bal=eff-((a.done||0)+(a.oih||0)),balCls=bal>=0?'cd-good':'cd-bad';
  // Done & Order-in-Hand cells drill to the underlying documents (see docDetailModal).
  var doneCls='cd-doc-cell'+((a.done||0)>0?'':' cd-doc-empty');
  var oihCls='cd-doc-cell'+((a.oih||0)>0?'':' cd-doc-empty');
  // Bal Realise = (target revenue − actual revenue) / balance litres, target taken as
  // the effective (flexible) target so it stays consistent with Bal.
  var balRlz=(eff>0&&bal!==0)?((eff*(a.targetRealise||0))-(a.lineTotal||0))/bal:NaN;
  var balRlzStr=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';
  var oihRlz=(a.oih||0)>0?((a.oihLineTotal||0)/a.oih):NaN;   // ₹/L of open orders
  var oihRlzStr=isFinite(oihRlz)?'₹'+fNp(oihRlz,2):'&mdash;';
  // Done Realise sits straight after Done L, so each litres figure is next to its
  // own realise. It used to be the last column, far from the litres it belongs to.
  return '<td>'+fN(a.target)+'</td>'
    +cdFlexCells(path,a)
    +'<td>'+fNp(a.targetRealise||0,2)+'</td>'
    +'<td class="'+doneCls+'" data-metric="done">'+fN(a.done)+'</td>'
    +'<td>'+fNp(a.actualRealise||0,2)+'</td>'
    +'<td class="'+oihCls+'" data-metric="oih">'+fN(a.oih)+'</td>'
    +'<td>'+oihRlzStr+'</td>'
    +'<td class="cd-bal-cell '+balCls+'">'+fN(bal)+'</td>'
    +'<td class="cd-balrlz-cell">'+balRlzStr+'</td>';
}
function cdNodeRow(node,depth){
  var meta=dimMeta(node.dim),twirl=node.leaf?'<span class="cd-twirl cd-leaf"></span>':'<span class="cd-twirl'+(cdExpanded[node.path]?' open':'')+'">&#9654;</span>';
  var tagHtml=meta.tag?'<span class="cd-tag '+meta.cls+'">'+esc(meta.tag)+'</span>':'';
  return '<tr class="'+(node.leaf?'cd-child':'cd-parent')+'"'+(node.leaf?'':' data-toggle="1" style="cursor:pointer"')+' data-path="'+esc(node.path)+'" data-name="'+esc(String(node.label||'').toLowerCase())+'">'
    +'<td style="padding-left:'+(14+depth*20)+'px">'+twirl+tagHtml+esc(node.label)+'</td>'
    +cdMetricCells(node.agg,node.path)+'</tr>';
}
// Channel-detail modal header search: filter rows by the first column (state/drill value).
// The TOTAL row always stays visible.
function cdFilterRows(input){
  var q=String(input.value||'').trim().toLowerCase();
  var rows=document.querySelectorAll('#cdBody tr');
  for(var i=0;i<rows.length;i++){
    var tr=rows[i];
    if(tr.classList.contains('cd-total')){tr.style.display='';continue;}
    var name=tr.getAttribute('data-name')||'';
    tr.style.display=(!q||name.indexOf(q)!==-1)?'':'none';
  }
}
// Flex TGT total over the visible top-level rows: total target − Σ(target − flex) of
// overridden top-level rows (the hidden '—' product/item bucket keeps its target).
function cdFlexTotal(totTarget){
  var dent=0;
  for(var i=0;i<cdTree.length;i++){
    var n=cdTree[i];
    if(cdFlex.hasOwnProperty(n.path))dent+=(Number(n.agg.target)||0)-(Number(cdFlex[n.path])||0);
  }
  return totTarget-dent;
}
// TOTAL-row cells: like cdMetricCells but the Flex TGT is a read-only computed sum, with
// Dent / Bal / Bal Rlz tracking it so the footer matches the column.
function cdMetricCellsTotal(a){
  var tgt=Number(a.target)||0, flexSum=cdFlexTotal(tgt);
  var dent=tgt-flexSum, dCls=dent>0?'cd-dent-pos':(dent<0?'cd-dent-neg':'');
  var bal=flexSum-((a.done||0)+(a.oih||0)), balCls=bal>=0?'cd-good':'cd-bad';
  var balRlz=(flexSum>0&&bal!==0)?((flexSum*(a.targetRealise||0))-(a.lineTotal||0))/bal:NaN;
  var balRlzStr=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';
  var doneCls='cd-doc-cell'+((a.done||0)>0?'':' cd-doc-empty');
  var oihCls='cd-doc-cell'+((a.oih||0)>0?'':' cd-doc-empty');
  var oihRlz=(a.oih||0)>0?((a.oihLineTotal||0)/a.oih):NaN;
  var oihRlzStr=isFinite(oihRlz)?'₹'+fNp(oihRlz,2):'&mdash;';
  return '<td>'+fN(a.target)+'</td>'
    +'<td class="cd-flexcol cd-flexin cd-flextot">'+fN(flexSum)+'</td>'
    +'<td class="cd-flexcol cd-dent '+dCls+'">'+(dent!==0?fN(dent):'&mdash;')+'</td>'
    +'<td>'+fNp(a.targetRealise||0,2)+'</td>'
    +'<td class="'+doneCls+'" data-metric="done">'+fN(a.done)+'</td>'
    +'<td class="'+oihCls+'" data-metric="oih">'+fN(a.oih)+'</td>'
    +'<td>'+oihRlzStr+'</td>'
    +'<td class="cd-bal-cell '+balCls+'">'+fN(bal)+'</td>'
    +'<td class="cd-balrlz-cell">'+balRlzStr+'</td>'
    +'<td>'+fNp(a.actualRealise||0,2)+'</td>';
}
// Refresh just the modal footer's Flex/Dent/Bal/Bal-Rlz after a per-row Flex TGT edit.
function cdRefreshTotal(){
  if(!cdTotalAgg)return;
  var totRow=document.querySelector('#cdBody tr.cd-total');
  if(!totRow)return;
  var tgt=Number(cdTotalAgg.target)||0, flexSum=cdFlexTotal(tgt);
  var bal=flexSum-((cdTotalAgg.done||0)+(cdTotalAgg.oih||0));
  var ft=totRow.querySelector('.cd-flextot'); if(ft)ft.innerHTML=fN(flexSum);
  var dc=totRow.querySelector('.cd-dent');
  if(dc){var d=tgt-flexSum;dc.innerHTML=(d!==0?fN(d):'&mdash;');dc.className='cd-flexcol cd-dent '+(d>0?'cd-dent-pos':(d<0?'cd-dent-neg':''));}
  var bc=totRow.querySelector('.cd-bal-cell');
  if(bc){bc.innerHTML=fN(bal);bc.className='cd-bal-cell '+(bal>=0?'cd-good':'cd-bad');}
  var br=totRow.querySelector('.cd-balrlz-cell');
  if(br){var rlz=(flexSum>0&&bal!==0)?((flexSum*(cdTotalAgg.targetRealise||0))-(cdTotalAgg.lineTotal||0))/bal:NaN;br.innerHTML=isFinite(rlz)?'₹'+fNp(rlz,2):'&mdash;';}
}
function cdTotalRow(leaves){
  cdNodeByPath['__cdtotal__']={filters:{},label:'TOTAL — '+(detailChannel||'')};
  cdTotalAgg=aggList(leaves);
  return '<tr class="cd-total" data-path="__cdtotal__"><td>TOTAL</td>'+cdMetricCellsTotal(cdTotalAgg)+'</tr>';
}
function cdTreeRows(nodes,depth){
  var out='';
  nodes.forEach(function(n){out+=cdNodeRow(n,depth);if(!n.leaf&&cdExpanded[n.path])out+=cdTreeRows(n.children,depth+1);});
  return out;
}

// ── Dynamic drill-order builder (same UX as the editor) ──
var DETAIL_DIMS=[
  {key:'group',name:'MAIN GROUP',sub:'GT / MT / ROI / HORECA'},
  {key:'state',name:'STATE',sub:'full state names'},
  {key:'person',name:'CONTACT PERSON',sub:'assigned territory owner'},
  {key:'product',name:'PRODUCT',sub:'sub-group / category'},
  {key:'item',name:'ITEM NAME',sub:'SAP item name'},
  {key:'customer',name:'CUSTOMER',sub:'CardName / buyer'}
];
var detailOrder=['state'], detailChosen=['state'];
// GT/MT/ROI already ARE a single main group, so only REST offers the Main Group level.
function detailDimsFor(channel){
  return channel==='REST'?DETAIL_DIMS:DETAIL_DIMS.filter(function(d){return d.key!=='group';});
}
function updateCdDrillLabel(){
  document.getElementById('cdDrillLabel').textContent=detailOrder.length?detailOrder.map(function(k){return dimMeta(k).label;}).join(' › '):'— none —';
}
function renderCdDpList(){
  var list=document.getElementById('cdDpList');list.innerHTML='';
  detailDimsFor(detailChannel).forEach(function(d){
    var pos=detailChosen.indexOf(d.key);
    var item=document.createElement('label');
    item.className='cd-dp-item'+(pos!==-1?' checked':'');
    item.innerHTML='<input type="checkbox" '+(pos!==-1?'checked':'')+'>'
      +'<span class="cd-dp-name">'+d.name+'<div class="cd-dp-sub">'+d.sub+'</div></span>'
      +'<span class="cd-dp-pos">'+(pos!==-1?pos+1:'')+'</span>';
    item.querySelector('input').addEventListener('change',function(e){
      if(e.target.checked){if(detailChosen.indexOf(d.key)===-1)detailChosen.push(d.key);}
      else{detailChosen=detailChosen.filter(function(k){return k!==d.key;});}
      renderCdDpList();
    });
    list.appendChild(item);
  });
}
function toggleCdDrill(e){e.stopPropagation();detailChosen=detailOrder.slice();renderCdDpList();document.getElementById('cdDrillPanel').classList.toggle('open');}
function cdSelectAll(){detailChosen=detailDimsFor(detailChannel).map(function(d){return d.key;});renderCdDpList();}
function cdClearAll(){detailChosen=[];renderCdDpList();}
function applyCdDrill(){
  if(!detailChosen.length){alert('Select at least one drill dimension.');return;}
  detailOrder=detailChosen.slice();
  cdExpanded={};
  document.getElementById('cdDrillPanel').classList.remove('open');
  updateCdDrillLabel();renderChannelDetail();
}
document.addEventListener('click',function(){var p=document.getElementById('cdDrillPanel');if(p)p.classList.remove('open');});
// Expand/collapse parent rows (delegated on document; survives re-renders).
document.addEventListener('click',function(e){
  if(!e.target||!e.target.closest)return;
  if(e.target.closest('#cdBody td.cd-doc-cell:not(.cd-doc-empty)'))return; // doc drill, not expand
  if(e.target.closest('#cdBody td.cd-flexcol'))return;                     // editing Flex TGT, not expand
  var tr=e.target.closest('#cdBody tr[data-toggle]');if(!tr)return;
  var p=tr.getAttribute('data-path');cdExpanded[p]=!cdExpanded[p];
  document.getElementById('cdBody').innerHTML=cdTreeRows(cdTree,0)+cdTotalRow(buildChannelDetailLeaves());
});
// Flexible View: live-update a row's Dent as its Flex TGT is typed. Done in-place (no
// full re-render) so the input keeps focus; the value is stashed in cdFlex by row path.
document.getElementById('cdBody').addEventListener('input',function(e){
  var inp=e.target&&e.target.closest?e.target.closest('input.cd-flex-input'):null;if(!inp)return;
  var rowEl=inp.closest('tr');if(!rowEl)return;
  var path=rowEl.getAttribute('data-path'),raw=String(inp.value).trim();
  var tgt=Number(inp.getAttribute('data-tgt'))||0;
  var done=Number(inp.getAttribute('data-done'))||0,oih=Number(inp.getAttribute('data-oih'))||0;
  var trate=Number(inp.getAttribute('data-tr'))||0,lt=Number(inp.getAttribute('data-lt'))||0;
  var cleared=(raw===''||isNaN(Number(raw))),fv=cleared?null:Number(raw);
  if(cleared)delete cdFlex[path]; else cdFlex[path]=fv;
  // Dent = Target − Flex TGT.
  var dent=rowEl.querySelector('.cd-dent');
  if(dent){
    if(cleared){dent.innerHTML='&mdash;';dent.className='cd-flexcol cd-dent';}
    else{var diff=tgt-fv;dent.innerHTML=fN(diff);dent.className='cd-flexcol cd-dent '+(diff>0?'cd-dent-pos':(diff<0?'cd-dent-neg':''));}
  }
  // Bal (and Bal Realise) recomputed on the effective target — flex if set, else original.
  var eff=cleared?tgt:fv, bal=eff-(done+oih);
  var balCell=rowEl.querySelector('.cd-bal-cell');
  if(balCell){balCell.innerHTML=fN(bal);balCell.className='cd-bal-cell '+(bal>=0?'cd-good':'cd-bad');}
  var brCell=rowEl.querySelector('.cd-balrlz-cell');
  if(brCell){var balRlz=(eff>0&&bal!==0)?((eff*trate)-lt)/bal:NaN;brCell.innerHTML=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';}
  cdRefreshTotal();   // keep the footer Flex / Dent / Bal in step with the edited row
});
// Click a Done-L / Order-in-Hand cell -> list the underlying invoices / open SOs.
document.addEventListener('click',function(e){
  if(!e.target||!e.target.closest)return;
  var cell=e.target.closest('#cdBody td.cd-doc-cell');
  if(!cell||cell.classList.contains('cd-doc-empty'))return;
  e.stopPropagation();
  var tr=cell.closest('tr'),path=tr?tr.getAttribute('data-path'):'';
  var node=path?cdNodeByPath[path]:null;
  openChannelDocs(cell.getAttribute('data-metric'),node?node.filters:{},node?node.label:'TOTAL');
});
async function openChannelDocs(metric,filters,label,opts){
  filters=filters||{}; opts=opts||{};
  var channel=opts.channel!==undefined?opts.channel:(detailChannel||'');
  var seg=opts.seg!==undefined?opts.seg:(detailSeg||'');
  var modal=document.getElementById('docDetailModal'),body=document.getElementById('docDetailBody');
  var isOih=metric==='oih';
  docMetric=metric;
  var metricLabel=isOih?'Order in Hand — open sales orders':'Done — invoices';
  document.getElementById('docDetailTitle').textContent=(label||'')+' · '+metricLabel;
  document.getElementById('docColDate').textContent=isOih?'SO Date':'Invoice Date';
  document.getElementById('docColNo').textContent=isOih?'SO No':'Invoice No';
  var crumbs=[];['group','state','person','product','item','customer'].forEach(function(k){if(filters[k])crumbs.push(dimMeta(k).label+': '+filters[k]);});
  document.getElementById('docDetailSub').textContent=crumbs.join('  ·  ')||((channel||'COMMODITY')+' — all');
  document.getElementById('docDetailCount').textContent='';
  document.getElementById('docDetailTot').textContent='';
  body.innerHTML='<tr><td colspan="6" class="cd-empty">Loading…</td></tr>';
  modal.classList.add('show');
  var params=new URLSearchParams();
  params.set('channel',channel||'');params.set('metric',metric||'done');params.set('seg',seg||'');
  var f=document.getElementById('sc2From'),t=document.getElementById('sc2To');
  if(f&&f.value)params.set('start',f.value);
  if(t&&t.value)params.set('end',t.value);
  Object.keys(filters).forEach(function(k){if(filters[k])params.set(k,filters[k]);});
  try{
    var res=await fetch(API+'/api/channel-detail-docs/?'+params.toString(),{headers:{'X-CSRFToken':getCSRF()}});
    var payload=res.ok?await res.json():{data:[]};
    docStockWarehouses=payload.warehouses||[];
    renderChannelDocs(payload.data||[]);
  }catch(err){
    body.innerHTML='<tr><td colspan="6" class="cd-empty">Could not load documents.</td></tr>';
  }
}
var docDetailRows=[], docExpanded={}, docPartyExpanded={}, docPartyGroups=[], docStockWarehouses=[], docMetric='oih';
// Group the flat document list by party so the popup opens grouped by customer; each party
// row carries the party total + outstanding balance and expands to its individual docs.
function docGroupByParty(rows){
  var map={}, order=[];
  for(var i=0;i<rows.length;i++){var r=rows[i], p=r.party||'—';
    var g=map[p]; if(!g){g=map[p]={party:p, balance:(r.balance!=null?r.balance:null), litres:0, docs:[], st:{}, ct:{}}; order.push(p);}
    g.litres+=Number(r.litres)||0; g.docs.push({r:r, idx:i});
    if(g.balance==null&&r.balance!=null)g.balance=r.balance;
    if(r.state)g.st[r.state]=1; if(r.city)g.ct[r.city]=1;
  }
  var groups=order.map(function(p){var g=map[p]; g.states=Object.keys(g.st); g.cities=Object.keys(g.ct); return g;});
  groups.sort(function(a,b){return b.litres-a.litres||String(a.party).localeCompare(String(b.party));});
  return groups;
}
function renderChannelDocs(rows){
  docDetailRows=rows||[]; docExpanded={}; docPartyExpanded={};
  docPartyGroups=docGroupByParty(docDetailRows);
  var body=document.getElementById('docDetailBody');
  if(!docDetailRows.length){body.innerHTML='<tr><td colspan="6" class="cd-empty">No documents in the current selection.</td></tr>';document.getElementById('docDetailCount').textContent='0 documents';document.getElementById('docDetailTot').textContent='';return;}
  var tot=0;for(var i=0;i<docDetailRows.length;i++)tot+=Number(docDetailRows[i].litres)||0;
  body.innerHTML=docDocsHtml();
  var np=docPartyGroups.length, nd=docDetailRows.length;
  document.getElementById('docDetailCount').textContent=np+(np===1?' party · ':' parties · ')+nd+(nd===1?' document':' documents');
  document.getElementById('docDetailTot').textContent=fN(tot);
}
// Party rows (collapsed by default) expand to their docs; each doc's Litres expands to line items.
function docDocsHtml(){
  var html='';
  for(var gi=0;gi<docPartyGroups.length;gi++){
    var g=docPartyGroups[gi], pkey=g.party, popen=!!docPartyExpanded[pkey], nDocs=g.docs.length;
    var balTag=(g.balance!=null)?'<span class="doc-bal" title="Customer outstanding balance (OCRD)">&#8377;'+fN(g.balance)+'</span>':'';
    var stTxt=g.states.length===1?g.states[0]:(g.states.length>1?'Multiple':'—');
    var ctTxt=g.cities.length===1?g.cities[0]:(g.cities.length>1?'Multiple':'—');
    var ptw='<span class="doc-twirl'+(popen?' open':'')+'">&#9654;</span>';
    html+='<tr class="doc-party-row" data-party="'+esc(pkey)+'"><td colspan="3">'+ptw+esc(g.party)+balTag+' <span class="doc-party-count">'+nDocs+(nDocs===1?' doc':' docs')+'</span></td>'
      +'<td>'+esc(stTxt)+'</td><td>'+esc(ctTxt)+'</td><td class="num">'+fN(g.litres)+'</td></tr>';
    if(!popen)continue;
    for(var di=0;di<g.docs.length;di++){
      var r=g.docs[di].r, idx=g.docs[di].idx, items=r.items||[], hasItems=items.length>0, open=!!docExpanded[idx];
      var litCls='doc-lit'+(hasItems?'':' doc-lit-empty');
      var twirl=hasItems?'<span class="doc-twirl'+(open?' open':'')+'">&#9654;</span>':'';
      html+='<tr class="doc-row doc-inv-row"><td>'+esc(r.doc_date||'—')+'</td><td>'+esc(r.doc_num||'—')+'</td><td></td><td></td><td></td>'
        +'<td class="num '+litCls+'" data-docidx="'+idx+'">'+twirl+fN(r.litres||0)+'</td></tr>';
      if(hasItems&&open){
        // OIH items carry per-warehouse on-hand stock; Done items are just name + litres.
        var detailed=items[0]&&items[0].stock!==undefined;
        if(detailed){
          html+='<tr class="doc-detail-row"><td colspan="6">'+docItemsTable(items)+'</td></tr>';
        }else{
          for(var k=0;k<items.length;k++){
            html+='<tr class="doc-item-row"><td></td><td colspan="4">'+esc(items[k].name||'—')+'</td><td class="num">'+fN(items[k].litres||0)+'</td></tr>';
          }
        }
      }
    }
  }
  return html;
}
// Nested item table for an OIH document: ITEM, one stock column per warehouse, OIH (L).
function docItemsTable(items){
  var whs=docStockWarehouses||[];
  var lastCol=docMetric==='oih'?'OIH (L)':'DONE (L)';
  var head='<tr><th>ITEM</th>';
  for(var w=0;w<whs.length;w++)head+='<th class="num">'+esc(whs[w])+'</th>';
  head+='<th class="num">'+lastCol+'</th></tr>';
  var body='';
  for(var i=0;i<items.length;i++){
    var it=items[i],st=it.stock||[];
    body+='<tr><td>'+esc(it.name||'—')+'</td>';
    for(var j=0;j<whs.length;j++)body+='<td class="num">'+fN(st[j]||0)+'</td>';
    body+='<td class="num">'+fN(it.litres||0)+'</td></tr>';
  }
  return '<table class="doc-items"><thead>'+head+'</thead><tbody>'+body+'</tbody></table>';
}
// Click a party row to expand its docs; click a doc's Litres cell to expand its line items.
document.addEventListener('click',function(e){
  if(!e.target||!e.target.closest)return;
  var prow=e.target.closest('#docDetailBody tr.doc-party-row');
  if(prow){
    var p=prow.getAttribute('data-party'); docPartyExpanded[p]=!docPartyExpanded[p];
    document.getElementById('docDetailBody').innerHTML=docDocsHtml();
    return;
  }
  var cell=e.target.closest('#docDetailBody td.doc-lit');
  if(!cell||cell.classList.contains('doc-lit-empty'))return;
  var idx=cell.getAttribute('data-docidx');
  docExpanded[idx]=!docExpanded[idx];
  document.getElementById('docDetailBody').innerHTML=docDocsHtml();
});
function closeChannelDocs(){document.getElementById('docDetailModal').classList.remove('show');}

/* ── Item-first refine filters ──────────────────────────────────────────────
   When the FIRST drill dimension is the Item, expose two multi-select dropdowns —
   Product (U_Sub_Group) and SKU (U_SKU pack size) — that narrow the rows before the
   breakdown / tree is built. Selections auto-clear when Item is no longer first.
   opts: {mountId, itemKey, prodField, skuField, getRows, getOrder, onChange}. */
function makeItemRefine(opts){
  var selProd={}, selSku={}, sig='', built=false;
  function norm(v){return String(v==null?'':v).trim().toUpperCase()||'—';}
  function fP(){return typeof opts.prodField==='function'?opts.prodField():opts.prodField;}   // Product field (may switch by company)
  function fS(){return typeof opts.skuField==='function'?opts.skuField():opts.skuField;}      // SKU field
  function firstIsItem(){var o=opts.getOrder()||[];return o.length>0 && o[0]===opts.itemKey;}
  function distinct(field){var seen={},rows=opts.getRows()||[];for(var i=0;i<rows.length;i++)seen[norm(rows[i][field])]=1;return Object.keys(seen).sort();}
  // Narrow rows to the chosen Products / SKUs — only when Item is the first dimension.
  function filterRows(rows){
    if(!firstIsItem())return rows;
    var hp=Object.keys(selProd).length, hs=Object.keys(selSku).length;
    if(!hp&&!hs)return rows;
    var pf=fP(), sf=fS();
    return (rows||[]).filter(function(r){return (!hp||selProd[norm(r[pf])])&&(!hs||selSku[norm(r[sf])]);});
  }
  function lbl(name,sel){var n=Object.keys(sel).length;return n?name+' ('+n+')':name;}
  function syncChecks(list,sel){list.querySelectorAll('.com-dp-item').forEach(function(it){var v=it.querySelector('.com-dp-name').textContent;var on=!!sel[v];it.querySelector('input').checked=on;it.classList.toggle('checked',on);});}
  function buildOne(name,field,sel){
    var vals=distinct(field);
    var box=document.createElement('div');box.className='com-drill-box';
    var btn=document.createElement('button');btn.type='button';btn.className='com-drill-btn ir-btn';
    btn.innerHTML='<span class="ir-lbl"></span><span class="com-caret">&#9662;</span>';
    btn.querySelector('.ir-lbl').textContent=lbl(name,sel);
    var panel=document.createElement('div');panel.className='com-drill-panel';
    var head=document.createElement('div');head.className='com-dp-head';
    var aAll=document.createElement('a');aAll.textContent='Select All';
    var aClr=document.createElement('a');aClr.textContent='Clear All';
    head.appendChild(aAll);head.appendChild(aClr);
    var list=document.createElement('div');list.className='com-dp-list';
    vals.forEach(function(v){
      var it=document.createElement('label');it.className='com-dp-item'+(sel[v]?' checked':'');
      it.innerHTML='<input type="checkbox" '+(sel[v]?'checked':'')+'><span class="com-dp-name">'+esc(v)+'</span>';
      it.querySelector('input').addEventListener('change',function(){
        if(this.checked)sel[v]=1;else delete sel[v];
        it.classList.toggle('checked',this.checked);
        btn.querySelector('.ir-lbl').textContent=lbl(name,sel);
        opts.onChange&&opts.onChange();
      });
      list.appendChild(it);
    });
    aAll.addEventListener('click',function(e){e.stopPropagation();vals.forEach(function(v){sel[v]=1;});syncChecks(list,sel);btn.querySelector('.ir-lbl').textContent=lbl(name,sel);opts.onChange&&opts.onChange();});
    aClr.addEventListener('click',function(e){e.stopPropagation();Object.keys(sel).forEach(function(k){delete sel[k];});syncChecks(list,sel);btn.querySelector('.ir-lbl').textContent=lbl(name,sel);opts.onChange&&opts.onChange();});
    btn.addEventListener('click',function(e){e.stopPropagation();var open=panel.classList.contains('open');var wrap=document.getElementById(opts.mountId);if(wrap)wrap.querySelectorAll('.com-drill-panel').forEach(function(p){p.classList.remove('open');});if(!open){var r=btn.getBoundingClientRect();panel.style.position='fixed';panel.style.top=(r.bottom+6)+'px';panel.style.left=r.left+'px';panel.style.zIndex='4000';panel.classList.add('open');}});
    panel.addEventListener('click',function(e){e.stopPropagation();});
    panel.appendChild(head);panel.appendChild(list);
    box.appendChild(btn);box.appendChild(panel);
    return box;
  }
  // (Re)build/hide the bar. Rebuilds only when Item-first status or the option sets
  // change, so the dropdowns keep their open state + ticks across panel re-renders.
  function sync(){
    var wrap=document.getElementById(opts.mountId);if(!wrap)return;
    if(!firstIsItem()){if(built||wrap.classList.contains('show')){wrap.classList.remove('show');wrap.innerHTML='';selProd={};selSku={};built=false;sig='';}return;}
    var newSig=(opts.getRows()||[]).length+'|'+distinct(fP()).join(',')+'|'+distinct(fS()).join(',');
    wrap.classList.add('show');
    if(built&&newSig===sig)return;
    sig=newSig;built=true;wrap.innerHTML='';
    var cap=document.createElement('span');cap.className='ir-cap';cap.textContent='Filter items ›';wrap.appendChild(cap);
    wrap.appendChild(buildOne('Product',fP(),selProd));
    wrap.appendChild(buildOne('SKU',fS(),selSku));
  }
  function reset(){selProd={};selSku={};built=false;sig='';var wrap=document.getElementById(opts.mountId);if(wrap){wrap.classList.remove('show');wrap.innerHTML='';}}
  return {sync:sync, filterRows:filterRows, reset:reset};
}
// Close any open refine dropdown when clicking outside it.
document.addEventListener('click',function(){document.querySelectorAll('.cd-refine .com-drill-panel.open').forEach(function(p){p.classList.remove('open');});});

/* ===== OIH KPI window: dynamic multi-level drill (U_Sub_Group / PackType / Item) ===== */
var OIH_DIM_NAME={main_group:'Main Group',state:'State',sub_group:'U_Sub_Group',packtype:'PackType',item:'Item Name',customer:'Customer Name'};
var oihDims=[{key:'main_group',label:'Main Group'},{key:'state',label:'State'},{key:'sub_group',label:'U_Sub_Group'},{key:'packtype',label:'PackType'},{key:'item',label:'Item Name'},{key:'customer',label:'Customer Name'}];
var oihOrder=['sub_group'], oihChosen=['sub_group'];
var oihKpiSeg='', oihGranular=[], oihError=null, oihExpanded={};
var oihItemStock={}, oihWhs=[];   // per-item on-hand stock (litres) by warehouse
function oihSegLabel(s){return s==='PREMIUM'?'Premium':(s==='COMMODITY'?'Commodity':'All segments');}
async function openOihWindow(){
  oihKpiSeg=sc2Seg();
  oihExpanded={};
  document.getElementById('oihKpiTitle').textContent='Order in Hand — '+oihSegLabel(oihKpiSeg);
  document.getElementById('oihKpiModal').classList.add('show');
  document.getElementById('oihKpiBody').innerHTML='<tr><td colspan="4" class="cd-empty">Loading…</td></tr>';
  try{
    var res=await fetch(API+'/api/oih-breakdown/',{headers:{'X-CSRFToken':getCSRF()}});
    var p=res.ok?await res.json():{};
    oihGranular=p.rows||[]; oihError=p.error||null;
    oihItemStock=p.item_stock||{}; oihWhs=p.warehouses||[];
    if(Array.isArray(p.dims)&&p.dims.length){oihDims=p.dims;}
  }catch(e){ oihGranular=[]; oihError='load failed'; }
  oihRefine.reset();
  oihUpdateDrillLabel();
  renderOihBreakdown();
}
function closeOihWindow(){document.getElementById('oihKpiModal').classList.remove('show');}
function oihKpiBox(label,val){return '<div class="oih-kpi-box"><span>'+label+'</span><b>'+fN(val||0)+'</b></div>';}
function oihDimLabel(k){return OIH_DIM_NAME[k]||k;}
function oihUpdateDrillLabel(){
  var el=document.getElementById('oihDrillLabel');
  if(el)el.textContent=oihOrder.length?oihOrder.map(oihDimLabel).join(' › '):'— none —';
}
// Nest granular rows into a tree per the chosen drill order; each node sums prem/comm.
function buildOihTree(rows,order){
  function group(subset,level){
    if(level>=order.length)return null;
    var dim=order[level],map={};
    for(var i=0;i<subset.length;i++){var r=subset[i],v=r[dim]||'—';
      if(!map[v])map[v]={name:v,dim:dim,premium:0,commodity:0,_rows:[]};
      map[v].premium+=r.premium||0; map[v].commodity+=r.commodity||0; map[v]._rows.push(r);
    }
    return Object.keys(map).map(function(k){var n=map[k];n.total=n.premium+n.commodity;n.kids=group(n._rows,level+1);delete n._rows;return n;})
      .sort(function(a,b){return b.total-a.total||String(a.name).localeCompare(String(b.name));});
  }
  return group(rows,0)||[];
}
function oihColCells(n,seg){
  if(seg==='PREMIUM')return '<td class="num">'+fN(n.premium)+'</td>';
  if(seg==='COMMODITY')return '<td class="num">'+fN(n.commodity)+'</td>';
  return '<td class="num">'+fN(n.premium)+'</td><td class="num">'+fN(n.commodity)+'</td><td class="num">'+fN(n.total)+'</td>';
}
// On-hand stock cells (one per warehouse) — only for item rows; blank otherwise.
function oihWhsCells(n,showWhs){
  if(!showWhs)return '';
  var st=(n&&n.dim==='item')?(oihItemStock[n.name]||null):null;
  var html='';
  for(var w=0;w<oihWhs.length;w++)html+='<td class="num">'+(st?fN(st[w]||0):'—')+'</td>';
  return html;
}
function oihTreeRows(nodes,level,seg,prefix,showWhs){
  var html='';
  for(var i=0;i<nodes.length;i++){var n=nodes[i];
    if(seg==='PREMIUM'&&n.premium<=0)continue;
    if(seg==='COMMODITY'&&n.commodity<=0)continue;
    var path=prefix+'>'+n.dim+':'+n.name, hasKids=n.kids&&n.kids.length>0, open=!!oihExpanded[path];
    var tw=hasKids?'<span class="oih-tw'+(open?' open':'')+'" data-oihpath="'+esc(path)+'">&#9654;</span>':'<span class="oih-tw-empty"></span>';
    html+='<tr><td style="padding-left:'+(12+level*18)+'px">'+tw+esc(n.name)+'</td>'+oihColCells(n,seg)+oihWhsCells(n,showWhs)+'</tr>';
    if(hasKids&&open)html+=oihTreeRows(n.kids,level+1,seg,path,showWhs);
  }
  return html;
}
var oihRefine=makeItemRefine({mountId:'oihRefine',itemKey:'item',prodField:'sub_group',skuField:'sku',
  getRows:function(){return oihGranular;},getOrder:function(){return oihOrder;},onChange:renderOihBreakdown});
function oihRows(){return oihRefine.filterRows(oihGranular);}   // Product/SKU-narrowed when Item is first
function renderOihBreakdown(){
  var seg=oihKpiSeg;
  var head=document.getElementById('oihKpiHead'), body=document.getElementById('oihKpiBody'), sum=document.getElementById('oihKpiSummary');
  var label=oihOrder.map(oihDimLabel).join(' › ')||'Dimension';
  oihRefine.sync();
  var frows=oihRows();
  // Show on-hand stock columns once the drill reaches the item level.
  var showWhs=oihOrder.indexOf('item')!==-1 && oihWhs.length>0;
  var whsHead=''; if(showWhs){for(var wi=0;wi<oihWhs.length;wi++)whsHead+='<th class="num">'+esc(oihWhs[wi])+'</th>';}
  // Totals (order-independent)
  var tp=0,tc=0; for(var i=0;i<frows.length;i++){tp+=frows[i].premium||0;tc+=frows[i].commodity||0;}
  tp=Math.round(tp*100)/100; tc=Math.round(tc*100)/100;
  if(seg==='PREMIUM')      sum.innerHTML=oihKpiBox('Premium OIH (L)',tp);
  else if(seg==='COMMODITY')sum.innerHTML=oihKpiBox('Commodity OIH (L)',tc);
  else                     sum.innerHTML=oihKpiBox('Premium OIH (L)',tp)+oihKpiBox('Commodity OIH (L)',tc)+oihKpiBox('Total OIH (L)',tp+tc);
  if(seg==='PREMIUM')      head.innerHTML='<th>'+esc(label)+'</th><th class="num">Premium (L)</th>'+whsHead;
  else if(seg==='COMMODITY')head.innerHTML='<th>'+esc(label)+'</th><th class="num">Commodity (L)</th>'+whsHead;
  else                     head.innerHTML='<th>'+esc(label)+'</th><th class="num">Premium (L)</th><th class="num">Commodity (L)</th><th class="num">Total (L)</th>'+whsHead;
  var span=(seg?2:4)+(showWhs?oihWhs.length:0);
  if(oihError){ body.innerHTML='<tr><td colspan="'+span+'" class="cd-empty">Could not load ('+esc(oihError)+').</td></tr>'; return; }
  var tree=buildOihTree(frows,oihOrder);
  var rowsHtml=oihTreeRows(tree,0,seg,'',showWhs);
  if(!rowsHtml){ body.innerHTML='<tr><td colspan="'+span+'" class="cd-empty">No open orders in this segment.</td></tr>'; return; }
  // TOTAL row
  var tot={premium:tp,commodity:tc,total:tp+tc};
  rowsHtml+='<tr class="cd-total"><td>TOTAL</td>'+oihColCells(tot,seg)+oihWhsCells(null,showWhs)+'</tr>';
  body.innerHTML=rowsHtml;
}
// Export the OIH breakdown (current Group By) as CSV (opens in Excel).
function exportOihKpiCSV(){
  if(!oihGranular||!oihGranular.length){showToast('Nothing to export','info');return;}
  var seg=oihKpiSeg, label=oihOrder.map(oihDimLabel).join(' > ')||'Dimension';
  var head=[label];
  if(seg==='PREMIUM')head.push('Premium (L)');
  else if(seg==='COMMODITY')head.push('Commodity (L)');
  else head.push('Premium (L)','Commodity (L)','Total (L)');
  var lines=[head.map(csvEscapeCell).join(',')];
  var frows=oihRows();
  function cols(n){return seg==='PREMIUM'?[n.premium||0]:(seg==='COMMODITY'?[n.commodity||0]:[n.premium||0,n.commodity||0,n.total||0]);}
  (function walk(nodes,depth){
    for(var i=0;i<nodes.length;i++){var n=nodes[i];
      lines.push([ (depth?new Array(depth+1).join('  '):'')+n.name ].concat(cols(n)).map(csvEscapeCell).join(','));
      if(n.kids&&n.kids.length)walk(n.kids,depth+1);
    }
  })(buildOihTree(frows,oihOrder),0);
  var tp=0,tc=0;for(var i=0;i<frows.length;i++){tp+=frows[i].premium||0;tc+=frows[i].commodity||0;}
  lines.push(['TOTAL'].concat(cols({premium:tp,commodity:tc,total:tp+tc})).map(csvEscapeCell).join(','));
  var blob=new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/csv;charset=utf-8;'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download=('Order_in_Hand_'+oihSegLabel(oihKpiSeg)).replace(/\s+/g,'_')+'.csv';a.click();
  setTimeout(function(){URL.revokeObjectURL(url);},1000);
  showToast('CSV exported','ok');
}
// Expand/collapse tree nodes
document.addEventListener('click',function(e){
  var b=e.target&&e.target.closest?e.target.closest('#oihKpiBody .oih-tw'):null;
  if(!b)return;
  var p=b.getAttribute('data-oihpath'); oihExpanded[p]=!oihExpanded[p];
  renderOihBreakdown();
});
// Drill-order builder panel (checkbox order, like the cards)
function oihRenderDpList(){
  var list=document.getElementById('oihDpList'); if(!list)return; list.innerHTML='';
  oihDims.forEach(function(d){
    var pos=oihChosen.indexOf(d.key);
    var item=document.createElement('label');
    item.className='com-dp-item'+(pos!==-1?' checked':'');
    item.innerHTML='<input type="checkbox" '+(pos!==-1?'checked':'')+'><span class="com-dp-name">'+esc(d.label)+'</span><span class="com-pos">'+(pos!==-1?pos+1:'')+'</span>';
    item.querySelector('input').addEventListener('change',function(){
      var i=oihChosen.indexOf(d.key);
      if(this.checked){ if(i===-1)oihChosen.push(d.key); } else if(i!==-1)oihChosen.splice(i,1);
      oihRenderDpList();
    });
    list.appendChild(item);
  });
}
function oihInitDrillPanel(){
  var btn=document.getElementById('oihDrillBtn'), panel=document.getElementById('oihDrillPanel');
  if(!btn||!panel)return;
  btn.addEventListener('click',function(e){ e.stopPropagation(); oihChosen=oihOrder.slice(); oihRenderDpList(); panel.classList.toggle('open'); });
  panel.addEventListener('click',function(e){ e.stopPropagation(); });
  document.addEventListener('click',function(){ panel.classList.remove('open'); });
  document.getElementById('oihSelAll').addEventListener('click',function(){ oihChosen=oihDims.map(function(d){return d.key;}); oihRenderDpList(); });
  document.getElementById('oihClrAll').addEventListener('click',function(){ oihChosen=[]; oihRenderDpList(); });
  document.getElementById('oihApply').addEventListener('click',function(){
    if(!oihChosen.length){ showToast('Pick at least one dimension','info'); return; }
    oihOrder=oihChosen.slice(); oihExpanded={};
    oihUpdateDrillLabel(); panel.classList.remove('open'); renderOihBreakdown();
  });
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',oihInitDrillPanel);
else oihInitDrillPanel();

/* ===== Done / Balance breakdown windows (Premium / Commodity / Total) =====
   The Done Ltr and Bal Ltr KPIs open a drill tree, mirroring the OIH window. Built
   entirely from slide-2 data already in memory:
     done    = +done litres (split Premium/Commodity by U_TYPE). Litres whose item
               carries no U_TYPE in SAP belong to neither column, so they get their own
               "Other" column - otherwise they landed in Total only and the row did not
               add up. The column is hidden when there are none.
     balance = target − done − OIH. Done/OIH split by U_TYPE. Targets mirror the
               dashboard's own views: the Premium column uses channel target nodes
               (segment PREMIUM or untagged), the Commodity column uses the per-product
               commodity targets, and Total counts every channel node once plus the
               commodity products. So Prem + Comm need not equal Total. */
var MB_DIMS=[{key:'main_group',label:'Main Group'},{key:'state',label:'State'},{key:'person',label:'Person'},{key:'sub_group',label:'U_Sub_Group'},{key:'item',label:'Item Name'},{key:'customer',label:'Customer Name'}];
var MB_DIM_NAME={main_group:'Main Group',state:'State',person:'Person',sub_group:'U_Sub_Group',item:'Item Name',customer:'Customer Name'};
var mbMetric='done', mbSeg='', mbOrder=['main_group'], mbChosen=['main_group'], mbExpanded={}, mbGranular=[];
function mbMetricLabel(){return mbMetric==='balance'?'Balance':'Done';}
function mbSegLabel(s){return s==='PREMIUM'?'Premium':(s==='COMMODITY'?'Commodity':'Premium & Commodity');}
function mbDimLabel(k){return MB_DIM_NAME[k]||k;}
function mbUpdateDrillLabel(){var el=document.getElementById('mbDrillLabel');if(el)el.textContent=mbOrder.length?mbOrder.map(mbDimLabel).join(' › '):'— none —';}
async function openMetricWindow(metric){
  if(!sc2Fetched){ showToast('Fetch the channel dashboard first','info'); return; }
  mbMetric=(metric==='balance')?'balance':'done';
  mbSeg=sc2Seg(); mbExpanded={};
  document.getElementById('mbTitle').textContent=mbMetricLabel()+' — '+mbSegLabel(mbSeg);
  document.getElementById('mbSubtitle').textContent=mbMetric==='balance'?'Target − Done − Order in Hand — litres':'Done litres — Premium & Commodity';
  document.getElementById('mbModal').classList.add('show');
  document.getElementById('mbBody').innerHTML='<tr><td colspan="4" class="cd-empty">Loading…</td></tr>';
  var targetNodes=[], oihRows=[];
  if(mbMetric==='balance'){
    var period=getResolvedChannelPeriod();
    try{ targetNodes=await fetchTargetNodes(period.month,period.year,''); }catch(e){ targetNodes=[]; }
    try{ oihRows=await fetchOrderInHandRows(); }catch(e){ oihRows=[]; }
  }
  mbGranular=buildMetricGranular(mbMetric,targetNodes,oihRows);
  mbRefine.reset();
  mbUpdateDrillLabel();
  renderMetricBreakdown();
}
function closeMetricWindow(){document.getElementById('mbModal').classList.remove('show');}
function buildMetricGranular(metric,targetNodes,oihRows){
  var g=[], rows=sc2Rows||[];
  function add(rawG,st,person,sub,item,cust,prem,comm,total,sku,other){
    g.push({main_group:rawG||'—',state:st||'—',person:person||'—',sub_group:sub||'—',item:item||'—',customer:cust||'—',sku:sku||'—',prem:prem,comm:comm,total:total,other:other||0});
  }
  var dSign=(metric==='balance')?-1:1;   // done is subtracted when computing balance
  for(var i=0;i<rows.length;i++){
    var r=rows[i], grp=String(r.u_main_group||'').trim().toUpperCase()||'—', st=String(r.state||'').trim().toUpperCase()||'—';
    var ut=String(r.u_type||'').toUpperCase(), v=(Number(r.liter)||0)*dSign;
    add(grp,st,assignedPerson(grp,st)||'—',String(r.u_sub_group||'').trim().toUpperCase(),String(r.item_name||'').trim().toUpperCase(),String(r.card_name||'').trim().toUpperCase(),ut==='PREMIUM'?v:0,ut==='COMMODITY'?v:0,v,String(r.sku||'').trim().toUpperCase(),(ut==='PREMIUM'||ut==='COMMODITY')?0:v);
  }
  if(metric==='balance'){
    for(var o=0;o<(oihRows||[]).length;o++){
      var x=oihRows[o], xg=String(x.main_group||'').trim().toUpperCase()||'—', xst=String(x.state||'').trim().toUpperCase()||'—';
      var xut=String(x.u_type||'').toUpperCase(), q=-(Number(x.open_qty)||0);
      add(xg,xst,String(x.sales_person||'').trim().toUpperCase()||assignedPerson(xg,xst)||'—',String(x.u_sub_group||'').trim().toUpperCase(),String(x.item_name||'').trim().toUpperCase(),String(x.card_name||'').trim().toUpperCase(),xut==='PREMIUM'?q:0,xut==='COMMODITY'?q:0,q,String(x.sku||'').trim().toUpperCase(),(xut==='PREMIUM'||xut==='COMMODITY')?0:q);
    }
    // Channel target nodes feed the Premium column (segment-tagged PREMIUM or untagged,
    // mirroring the dashboard's Premium view) and count once toward Total. Commodity is
    // owned per product (added next), so channel nodes don't feed the Commodity column.
    for(var t=0;t<(targetNodes||[]).length;t++){
      var n=targetNodes[t], ng=String(n.main_group||'').trim().toUpperCase()||'—', nst=String(n.state||'').trim().toUpperCase()||'—';
      var tl=Number(n.target_ltrs)||0; if(!tl)continue;
      var nseg=String(n.segment||'').toUpperCase();
      var pr=(nseg===''||nseg==='PREMIUM')?tl:0;
      add(ng,nst,String(n.sales_person||'').trim().toUpperCase()||assignedPerson(ng,nst)||'—','—','—','—',pr,0,tl);
    }
    // Commodity per-product targets (Surjeet Singh's store) feed the Commodity column
    // and Total; they carry no state/person, so they bucket by product (U_Sub_Group).
    var comSubs=commoditySubGroups();
    for(var cs=0;cs<comSubs.length;cs++){
      var csub=comSubs[cs], ctl=Number(getDefTS('COMMODITY',csub))||0; if(!ctl)continue;
      add('—','—','—',csub,'—','—',0,ctl,ctl);
    }
  }
  return g;
}
function buildMbTree(rows,order){
  function group(subset,level){
    if(level>=order.length)return null;
    var dim=order[level],map={};
    for(var i=0;i<subset.length;i++){var r=subset[i],v=r[dim]||'—';
      if(!map[v])map[v]={name:v,dim:dim,prem:0,comm:0,other:0,total:0,_rows:[]};
      map[v].prem+=r.prem||0;map[v].comm+=r.comm||0;map[v].other+=r.other||0;map[v].total+=r.total||0;map[v]._rows.push(r);}
    return Object.keys(map).map(function(k){var n=map[k];n.kids=group(n._rows,level+1);delete n._rows;return n;})
      .sort(function(a,b){return b.total-a.total||String(a.name).localeCompare(String(b.name));});
  }
  return group(rows,0)||[];
}
function mbNum(v){var s=fN(v||0);return ((v||0)<0)?'<span class="sc2-bal-bad">'+s+'</span>':s;}
// Shown only when some litres really are untagged, so the usual view is unchanged.
var mbShowOther=false;
function mbColCells(n,seg){
  if(seg==='PREMIUM')return '<td class="num">'+mbNum(n.prem)+'</td>';
  if(seg==='COMMODITY')return '<td class="num">'+mbNum(n.comm)+'</td>';
  return '<td class="num">'+mbNum(n.prem)+'</td><td class="num">'+mbNum(n.comm)+'</td>'
        +(mbShowOther?'<td class="num">'+mbNum(n.other)+'</td>':'')
        +'<td class="num">'+mbNum(n.total)+'</td>';
}
function mbTreeRows(nodes,level,seg,prefix){
  var html='';
  for(var i=0;i<nodes.length;i++){var n=nodes[i];
    var path=prefix+'>'+n.dim+':'+n.name, hasKids=n.kids&&n.kids.length>0, open=!!mbExpanded[path];
    var tw=hasKids?'<span class="oih-tw'+(open?' open':'')+'" data-mbpath="'+esc(path)+'">&#9654;</span>':'<span class="oih-tw-empty"></span>';
    html+='<tr><td style="padding-left:'+(12+level*18)+'px">'+tw+esc(n.name)+'</td>'+mbColCells(n,seg)+'</tr>';
    if(hasKids&&open)html+=mbTreeRows(n.kids,level+1,seg,path);
  }
  return html;
}
var mbRefine=makeItemRefine({mountId:'mbRefine',itemKey:'item',prodField:'sub_group',skuField:'sku',
  getRows:function(){return mbGranular;},getOrder:function(){return mbOrder;},onChange:renderMetricBreakdown});
function mbRows(){return mbRefine.filterRows(mbGranular);}   // Product/SKU-narrowed when Item is first
function renderMetricBreakdown(){
  var seg=mbSeg, metricL=mbMetricLabel();
  var head=document.getElementById('mbHead'), body=document.getElementById('mbBody'), sum=document.getElementById('mbSummary');
  var label=mbOrder.map(mbDimLabel).join(' › ')||'Dimension';
  mbRefine.sync();
  var frows=mbRows();
  var tp=0,tc=0,to=0,tt=0; for(var i=0;i<frows.length;i++){tp+=frows[i].prem||0;tc+=frows[i].comm||0;to+=frows[i].other||0;tt+=frows[i].total||0;}
  tp=Math.round(tp*100)/100;tc=Math.round(tc*100)/100;to=Math.round(to*100)/100;tt=Math.round(tt*100)/100;
  // Only shown when some litres really are untagged, so a clean month looks exactly as it
  // did before. Done only: the Balance metric counts targets in Total on purpose (see the
  // note above buildMetricGranular), so its columns never reconciled anyway.
  mbShowOther=(seg==='')&&(mbMetric==='done')&&(to!==0);
  var otherTip='Litres whose item has no U_TYPE set in SAP, so they are neither Premium nor Commodity. '
              +'They were always inside Total - this column just shows them, so the row adds up.';
  if(seg==='PREMIUM')sum.innerHTML=oihKpiBox('Premium '+metricL+' (L)',tp);
  else if(seg==='COMMODITY')sum.innerHTML=oihKpiBox('Commodity '+metricL+' (L)',tc);
  else sum.innerHTML=oihKpiBox('Premium '+metricL+' (L)',tp)+oihKpiBox('Commodity '+metricL+' (L)',tc)
                    +(mbShowOther?oihKpiBox('Other — no type (L)',to):'')
                    +oihKpiBox('Total '+metricL+' (L)',tt);
  if(seg==='PREMIUM')head.innerHTML='<th>'+esc(label)+'</th><th class="num">Premium (L)</th>';
  else if(seg==='COMMODITY')head.innerHTML='<th>'+esc(label)+'</th><th class="num">Commodity (L)</th>';
  else head.innerHTML='<th>'+esc(label)+'</th><th class="num">Premium (L)</th><th class="num">Commodity (L)</th>'
                     +(mbShowOther?'<th class="num" title="'+esc(otherTip)+'">Other (L)</th>':'')
                     +'<th class="num">Total (L)</th>';
  var span=seg?2:(mbShowOther?5:4);
  var tree=buildMbTree(frows,mbOrder);
  var rowsHtml=mbTreeRows(tree,0,seg,'');
  if(!rowsHtml){ body.innerHTML='<tr><td colspan="'+span+'" class="cd-empty">No data in this selection.</td></tr>'; return; }
  rowsHtml+='<tr class="cd-total"><td>TOTAL</td>'+mbColCells({prem:tp,comm:tc,other:to,total:tt},seg)+'</tr>';
  body.innerHTML=rowsHtml;
}
document.addEventListener('click',function(e){
  var b=e.target&&e.target.closest?e.target.closest('#mbBody .oih-tw'):null;
  if(!b)return;
  var p=b.getAttribute('data-mbpath'); mbExpanded[p]=!mbExpanded[p]; renderMetricBreakdown();
});
// Export the metric breakdown (current Group By) as CSV (opens in Excel).
function exportMetricCSV(){
  if(!mbGranular||!mbGranular.length){showToast('Nothing to export','info');return;}
  var seg=mbSeg, label=mbOrder.map(mbDimLabel).join(' > ')||'Dimension';
  var head=[label];
  if(seg==='PREMIUM')head.push('Premium (L)');
  else if(seg==='COMMODITY')head.push('Commodity (L)');
  else head.push.apply(head,mbShowOther?['Premium (L)','Commodity (L)','Other — no type (L)','Total (L)']
                                       :['Premium (L)','Commodity (L)','Total (L)']);
  var lines=[head.map(csvEscapeCell).join(',')];
  var frows=mbRows();
  function cols(n){
    if(seg==='PREMIUM')return [n.prem||0];
    if(seg==='COMMODITY')return [n.comm||0];
    return mbShowOther?[n.prem||0,n.comm||0,n.other||0,n.total||0]:[n.prem||0,n.comm||0,n.total||0];
  }
  (function walk(nodes,depth){
    for(var i=0;i<nodes.length;i++){var n=nodes[i];
      lines.push([ (depth?new Array(depth+1).join('  '):'')+n.name ].concat(cols(n)).map(csvEscapeCell).join(','));
      if(n.kids&&n.kids.length)walk(n.kids,depth+1);
    }
  })(buildMbTree(frows,mbOrder),0);
  var tp=0,tc=0,tt=0;for(var i=0;i<frows.length;i++){tp+=frows[i].prem||0;tc+=frows[i].comm||0;tt+=frows[i].total||0;}
  lines.push(['TOTAL'].concat(cols({prem:tp,comm:tc,total:tt})).map(csvEscapeCell).join(','));
  var blob=new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/csv;charset=utf-8;'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download=(mbMetricLabel()+'_'+mbSegLabel(mbSeg)).replace(/\s+/g,'_')+'.csv';a.click();
  setTimeout(function(){URL.revokeObjectURL(url);},1000);
  showToast('CSV exported','ok');
}
function mbRenderDpList(){
  var list=document.getElementById('mbDpList'); if(!list)return; list.innerHTML='';
  MB_DIMS.forEach(function(d){
    var pos=mbChosen.indexOf(d.key);
    var item=document.createElement('label');
    item.className='com-dp-item'+(pos!==-1?' checked':'');
    item.innerHTML='<input type="checkbox" '+(pos!==-1?'checked':'')+'><span class="com-dp-name">'+esc(d.label)+'</span><span class="com-pos">'+(pos!==-1?pos+1:'')+'</span>';
    item.querySelector('input').addEventListener('change',function(){
      var i=mbChosen.indexOf(d.key);
      if(this.checked){ if(i===-1)mbChosen.push(d.key); } else if(i!==-1)mbChosen.splice(i,1);
      mbRenderDpList();
    });
    list.appendChild(item);
  });
}
function mbInitDrillPanel(){
  var btn=document.getElementById('mbDrillBtn'), panel=document.getElementById('mbDrillPanel');
  if(!btn||!panel)return;
  btn.addEventListener('click',function(e){ e.stopPropagation(); mbChosen=mbOrder.slice(); mbRenderDpList(); panel.classList.toggle('open'); });
  panel.addEventListener('click',function(e){ e.stopPropagation(); });
  document.addEventListener('click',function(){ panel.classList.remove('open'); });
  document.getElementById('mbSelAll').addEventListener('click',function(){ mbChosen=MB_DIMS.map(function(d){return d.key;}); mbRenderDpList(); });
  document.getElementById('mbClrAll').addEventListener('click',function(){ mbChosen=[]; mbRenderDpList(); });
  document.getElementById('mbApply').addEventListener('click',function(){
    if(!mbChosen.length){ showToast('Pick at least one dimension','info'); return; }
    mbOrder=mbChosen.slice(); mbExpanded={};
    mbUpdateDrillLabel(); panel.classList.remove('open'); renderMetricBreakdown();
  });
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mbInitDrillPanel);
else mbInitDrillPanel();

var cdRefineLeaves=[];
var cdRefine=makeItemRefine({mountId:'cdRefine',itemKey:'item',prodField:'product',skuField:'sku',
  getRows:function(){return cdRefineLeaves;},getOrder:function(){return detailOrder;},onChange:renderChannelDetail});
function renderChannelDetail(){
  var dims=detailOrder.slice();
  var body=document.getElementById('cdBody');
  var cc=(cdViewMode==='flex')?11:9;   // +2 visible columns (Flex TGT, Dent) in Flexible View
  if(!dims.length){document.getElementById('cdFirstCol').textContent='—';cdRefine.sync();body.innerHTML='<tr><td colspan="'+cc+'" class="cd-empty">Select at least one drill dimension.</td></tr>';return;}
  document.getElementById('cdFirstCol').textContent=dims.map(function(d){return dimMeta(d).label;}).join(' › ');
  try{
    var leaves=buildChannelDetailLeaves();
    if(!leaves.length){cdTree=[];cdRefineLeaves=[];cdRefine.sync();body.innerHTML='<tr><td colspan="'+cc+'" class="cd-empty">No data for this channel in the current selection.</td></tr>';return;}
    cdRefineLeaves=leaves;                 // full set feeds the Product/SKU option lists
    cdRefine.sync();
    leaves=cdRefine.filterRows(leaves);    // narrowed when Item is the first drill dimension
    if(!leaves.length){cdTree=[];body.innerHTML='<tr><td colspan="'+cc+'" class="cd-empty">No rows match the selected Product / SKU filter.</td></tr>';return;}
    cdNodeByPath={};
    cdTree=buildDetailTree(leaves,dims,0,'');
    body.innerHTML=cdTreeRows(cdTree,0)+cdTotalRow(leaves);
  }catch(err){body.innerHTML='<tr><td colspan="'+cc+'" class="cd-empty">Could not render: '+esc(String((err&&err.message)||err))+'</td></tr>';}
}

async function renderSlideTwo(){
  var grid=document.getElementById('channelGrid');
  if(!grid)return;
  if(!sc2Fetched){
    setSlideTwoKpis(0,0,0,0);
    grid.innerHTML='<div class="sc2-empty" style="grid-column:1 / -1">Pick a date range and click Fetch to populate the channel dashboard.</div>';
    return;
  }
  sc2Rendered=true;
  var seg=(document.getElementById('sc2Segment')||{}).value||'';
  applySlideTwoSegmentUI(seg);
  if(seg==='COMMODITY'){ renderSlideTwoCommodity(); return; }
  var period=getResolvedChannelPeriod();
  var rows=getFilteredChannelRows();
  var mode=getSlideTwoDrillBy();
  var groupFilter=getSlideTwoGroupFilter();

  if(mode!=='main_group'){
    var dTargetNodes=[], dOihRows=[];
    try{ dTargetNodes=await fetchTargetNodes(period.month,period.year); }catch(e){ dTargetNodes=[]; }
    try{ dOihRows=await fetchOrderInHandRows(); }catch(e){ dOihRows=[]; }
    if(!sc2DynUserSet||!sc2DynOrder.length){ sc2DynOrder=[mode]; sc2DynUpdateLabel(); }
    var leaves=buildSc2DynLeaves(rows,dTargetNodes,dOihRows);
    sc2DynLastLeaves=leaves;
    var dt=aggList(leaves);
    await loadFlexTargets(period.month,period.year);   // load saved Flex TGT before render + KPI
    grid.innerHTML=renderSc2DynTree(leaves);           // builds sc2DynTree + sc2DynTotalAgg
    var dKpiTarget=dt.target+(sc2Seg()===''?commodityTargetTotal():0);
    // same fold-in as the card view, so the KPI strip agrees in both modes
    var dTrSum=(Number(dt.targetRealise)||0)*(Number(dt.target)||0), dTrW=Number(dt.target)||0;
    if(sc2Seg()===''){ var dcp=commodityTargetRealiseParts(); dTrSum+=dcp.sum; dTrW+=dcp.w; }
    var dKpiTargetRealise=dTrW>0?dTrSum/dTrW:0;
    // Bal Ltr KPI = the TOTAL row's "Bal" = flex-adjusted target − Done − OIH (so it matches).
    var dBal=sc2DynFlexTotal(dt.target)-((dt.done||0)+(dt.oih||0));
    setSlideTwoKpis(dKpiTarget,dt.done,dKpiTargetRealise,sc2Realise(rows),dt.oih,dBal,
                    (dt.oih||0)>0?((dt.oihLineTotal||0)/dt.oih):0);
    sc2CsvRows=sc2DynCsvRows(sc2DynTree,dt);
    sc2DynFitHeight();                         // grow the body to fill the slide
    return;
  }

  var targetNodes=[], oihRows=[];
  try{ targetNodes=await fetchTargetNodes(period.month,period.year); }catch(e){ targetNodes=[]; }
  try{ oihRows=await fetchOrderInHandRows(); }catch(e){ oihRows=[]; }
  var cardByName={}, crByName={}, agg={target:0,done:0,oih:0,lineTotal:0,oihLineTotal:0,trW:0,trWsum:0};
  // E-Com takes REST's old grid slot (4th cell); Horeca, CSD then REST flow below in
  // the same 2-col grid, which scrolls. Keep in sync with CHANNEL_BLOCKS.
  var layoutNames=['GT','ROI','MT','ECOM','HORECA','CSD','REST'];
  for(var bi=0;bi<CHANNEL_BLOCKS.length;bi++){
    var block=CHANNEL_BLOCKS[bi], name=block.name;
    var cr=buildCardRows(name,block.members,rows,targetNodes,oihRows);
    var ct=cardRowsTotal(cr);
    crByName[name]={rows:cr, total:ct, label:name==='REST'?'Main Group':'State/Area'};
    agg.target+=ct.target; agg.done+=ct.done; agg.oih+=ct.oih; agg.lineTotal+=ct.lineTotal;
    agg.oihLineTotal+=ct.oihLineTotal||0;   // open-order value, for the OIH realise line
    if(ct.target>0){ agg.trWsum+=ct.targetRealise*ct.target; agg.trW+=ct.target; }
    cardByName[name]=themedChannelCardHtml({
      name:name, members:block.members,
      target:ct.target, done:ct.done, oih:ct.oih, oihLineTotal:ct.oihLineTotal,
      lineTotal:ct.lineTotal, targetRealise:ct.targetRealise, actualRealise:ct.actualRealise,
      progress:ct.target>0?(ct.done/ct.target)*100:0,
      firstColLabel:name==='REST'?'Main Group':'State/Area',
      rowsData:cr
    });
  }
  /* Weight the target realise over the SAME litres the Target Ltr card shows -
     channel targets plus, when no segment is picked, the commodity targets. */
  var trSum=agg.trWsum, trW=agg.trW;
  if(sc2Seg()===''){ var cp=commodityTargetRealiseParts(); trSum+=cp.sum; trW+=cp.w; }
  var kpiTargetRealise=trW>0?trSum/trW:0;
  var kpiTarget=agg.target+(sc2Seg()===''?commodityTargetTotal():0);
  // balLtr stays undefined on purpose so it keeps defaulting to Target - Done - OIH.
  setSlideTwoKpis(kpiTarget,agg.done,kpiTargetRealise,sc2Realise(rows),agg.oih,undefined,
                  (agg.oih||0)>0?((agg.oihLineTotal||0)/agg.oih):0);
  sc2CsvRows=buildChannelCsvRows(layoutNames,crByName,agg,sc2Realise(rows));
  var html='';
  for(var li=0;li<layoutNames.length;li++){ if(cardByName[layoutNames[li]])html+=cardByName[layoutNames[li]]; }
  grid.innerHTML=html;
}
function sc2Realise(rows){var l=0,lt=0;for(var i=0;i<rows.length;i++){l+=Number(rows[i].liter)||0;lt+=Number(rows[i].line_total)||0;}return l>0?lt/l:0;}
// Per-row metrics for a channel card: ltrs target/done, line total (revenue),
// target realise (litres-weighted ₹/L) and actual realise (revenue/litres).
function buildCardRows(channel,members,rows,targetNodes,oihRows){
  var isREST=channel==='REST', byKey={};
  function cell(k){ if(!byKey[k])byKey[k]={name:k,target:0,done:0,oih:0,lineTotal:0,oihLineTotal:0,trWsum:0,trW:0}; return byKey[k]; }
  if(!isREST && CHANNEL_STATE_WHITELIST[channel]){ CHANNEL_STATE_WHITELIST[channel].forEach(function(w){cell(w.label);}); }
  for(var i=0;i<rows.length;i++){
    var r=rows[i],g=String(r.u_main_group||'').toUpperCase();
    if(members.indexOf(g)===-1)continue;
    var k=isREST?g:detailStateLabel(channel,r.state);
    if(k===null)continue;
    var c=cell(k); c.done+=Number(r.liter)||0; c.lineTotal+=Number(r.line_total)||0;
  }
  for(var t=0;t<targetNodes.length;t++){
    var n=targetNodes[t],ng=String(n.main_group||'').toUpperCase();
    var isChanLvl=(ng===String(channel||'').toUpperCase());   // channel-total target: main_group=<channel>
    if(!isChanLvl && members.indexOf(ng)===-1)continue;
    var k2=isREST?ng:((isChanLvl && !String(n.state||'').trim())?CD_WHOLE:detailStateLabel(channel,n.state));
    if(k2===null)continue;
    var c2=cell(k2),tl=Number(n.target_ltrs)||0,tr=Number(n.target_realise)||0;
    c2.target+=tl;
    if(tr>0){ var wt=tl>0?tl:1; c2.trWsum+=tr*wt; c2.trW+=wt; }
  }
  var oihSeg=sc2Seg();
  for(var o=0;o<(oihRows||[]).length;o++){
    var x=oihRows[o],xg=String(x.main_group||'').toUpperCase();
    if(members.indexOf(xg)===-1)continue;
    if(oihSeg&&String(x.u_type||'').toUpperCase()!==oihSeg)continue;   // honour segment
    var k3=isREST?xg:detailStateLabel(channel,x.state);
    if(k3===null)continue;
    var oc=cell(k3); oc.oih+=Number(x.open_qty)||0; oc.oihLineTotal+=Number(x.open_value)||0;
  }
  var list=Object.keys(byKey).map(function(k){
    var c=byKey[k];
    c.targetRealise=c.trW>0?c.trWsum/c.trW:0;
    c.actualRealise=c.done>0?c.lineTotal/c.done:0;
    return c;
  });
  if(!isREST && CHANNEL_STATE_WHITELIST[channel]){
    var ord={},wl=CHANNEL_STATE_WHITELIST[channel];for(var w=0;w<wl.length;w++)ord[wl[w].label]=w;
    list.sort(function(a,b){return (ord[a.name]==null?99:ord[a.name])-(ord[b.name]==null?99:ord[b.name]);});
  }else list.sort(function(a,b){return b.done-a.done||a.name.localeCompare(b.name);});
  return list;
}
function cardRowsTotal(list){
  var t={target:0,done:0,oih:0,lineTotal:0,oihLineTotal:0,trWsum:0,trW:0};
  list.forEach(function(c){ t.target+=c.target; t.done+=c.done; t.oih+=c.oih; t.lineTotal+=c.lineTotal; t.oihLineTotal+=c.oihLineTotal||0;
    if(c.targetRealise>0&&c.target>0){t.trWsum+=c.targetRealise*c.target;t.trW+=c.target;} });
  t.targetRealise=t.trW>0?t.trWsum/t.trW:0;
  t.actualRealise=t.done>0?t.lineTotal/t.done:0;
  return t;
}

/* ===== Slide-2 main-table DYNAMIC DRILLER (same UX as the channel-detail cards) =====
   When Drill By = Sales Person / State, the main table becomes a reorderable,
   collapsible drill tree across ALL channels. Dimensions: Main Group, State,
   Person, Product, Item, Customer (reuses DETAIL_DIMS / aggList / dimMeta).
   Targets carry no product/item/customer breakdown -> they bucket under '—'
   (hidden in those drills but always counted in TOTAL, like the card driller). */
var SC2_DYN_DIMS=DETAIL_DIMS;
var sc2DynOrder=['state'], sc2DynChosen=['state'];
var sc2DynUserSet=false;   // true once the user applies a custom order (don't reseed from Drill By)
var sc2DynExpanded={}, sc2DynNodeByPath={}, sc2DynTree=[], sc2DynLastLeaves=null, sc2DynTotalAgg=null;
var SC2_DYN_GCOLS='grid-template-columns:1.6fr .8fr .8fr .8fr .8fr .9fr .9fr';
// Rows that can't be attributed to a real person / customer / product / item land
// in the '—' bucket; show that as "Other" (UNKNOWN states keep their own label).
function sc2DynLabel(v){ return v==='—'?'Other':v; }

function buildSc2DynLeaves(rows,targetNodes,oihRows){
  var map={};
  function cell(group,state,person,customer,product,item){
    var k=group+'|'+state+'|'+person+'|'+customer+'|'+product+'|'+item;
    if(!map[k])map[k]={group:group,state:state,person:person,customer:customer,product:product,item:item,
      target:0,done:0,oih:0,lineTotal:0,targetRealise:0};
    return map[k];
  }
  for(var i=0;i<rows.length;i++){
    var r=rows[i], rawG=String(r.u_main_group||'').toUpperCase();
    var g=normaliseChannelGroup(rawG)||'—';
    var st=canonState(r.state)||'—';
    var p=assignedPerson(rawG,r.state)||rawSpPerson(r.sales_person)||'—';   // territory owner → real raw SP → Other
    var cust=String(r.card_name||'').trim().toUpperCase()||'—';
    var prod=String(r.u_sub_group||'').trim().toUpperCase()||'—';
    var it=String(r.item_name||'').trim().toUpperCase()||'—';
    var c=cell(g,st,p,cust,prod,it); c.done+=Number(r.liter)||0; c.lineTotal+=Number(r.line_total)||0;
  }
  for(var t=0;t<(targetNodes||[]).length;t++){
    var n=targetNodes[t], rawNg=String(n.main_group||'').toUpperCase();
    var g2=normaliseChannelGroup(rawNg)||'—';
    var st2=canonState(n.state)||'—';
    var p2=String(n.sales_person||'').toUpperCase()||assignedPerson(rawNg,n.state)||'—';
    var c2=cell(g2,st2,p2,'—','—','—'); c2.target+=Number(n.target_ltrs)||0;
    var tr=Number(n.target_realise)||0; if(tr>0)c2.targetRealise=tr;
  }
  var oihSeg=sc2Seg();
  for(var o=0;o<(oihRows||[]).length;o++){
    var x=oihRows[o], rawXg=String(x.main_group||'').toUpperCase();
    if(oihSeg&&String(x.u_type||'').toUpperCase()!==oihSeg)continue;   // honour segment
    var g3=normaliseChannelGroup(rawXg)||'—';
    var st3=canonState(x.state)||'—';
    var p3=assignedPerson(rawXg,x.state)||rawSpPerson(x.sales_person)||'—';   // match the Done resolution
    var cust3=String(x.card_name||'').trim().toUpperCase()||'—';
    var prod3=String(x.u_sub_group||'').trim().toUpperCase()||'—';
    var it3=String(x.item_name||'').trim().toUpperCase()||'—';
    cell(g3,st3,p3,cust3,prod3,it3).oih+=Number(x.open_qty)||0;
  }
  return Object.keys(map).map(function(k){return map[k];});
}
function sc2DynOrderValues(dim,m){
  var names=Object.keys(m);
  if(dim==='person')return orderPersons(names);
  if(dim==='group'){var ord={GT:0,ROI:1,MT:2,ECOM:3,HORECA:4,CSD:5,REST:6};return names.sort(function(a,b){return (ord[a]==null?9:ord[a])-(ord[b]==null?9:ord[b])||a.localeCompare(b);});}
  if(dim==='state')return names.sort(function(a,b){return a.localeCompare(b);});
  return names.sort(function(a,b){return aggList(m[b]).done-aggList(m[a]).done||a.localeCompare(b);}); // product/item/customer by done desc
}
function sc2DynBuildTree(leaves,dims,depth,prefix,filt){
  filt=filt||{};
  var d=dims[depth], m=groupLeavesBy(leaves,d), nodes=[];
  sc2DynOrderValues(d,m).forEach(function(v){
    if((d==='product'||d==='item')&&v==='—')return;   // hide empty target bucket; TOTAL still counts it
    var path=prefix+'¦'+d+'='+v;
    var nodeFilt={};for(var fk in filt)nodeFilt[fk]=filt[fk];nodeFilt[d]=v;
    var node={path:path,dim:d,label:v,filters:nodeFilt,agg:aggList(m[v]),leaf:depth+1>=dims.length,children:[]};
    sc2DynNodeByPath[path]=node;
    if(!node.leaf)node.children=sc2DynBuildTree(m[v],dims,depth+1,path,nodeFilt);
    nodes.push(node);
  });
  return nodes;
}
function sc2DynNodeRow(node,depth){
  var meta=dimMeta(node.dim);
  var twirl=node.leaf?'<span class="cd-twirl cd-leaf"></span>':'<span class="cd-twirl'+(sc2DynExpanded[node.path]?' open':'')+'">&#9654;</span>';
  var tag=meta.tag?'<span class="cd-tag '+meta.cls+'">'+esc(meta.tag)+'</span>':'';
  var name='<div class="sc2-drillname" style="padding-left:'+(depth*18)+'px">'+twirl+tag+esc(sc2DynLabel(node.label))+'</div>';
  var rowStyle=sc2DynGcols()+(node.leaf?'':';cursor:pointer');
  return '<div class="sc2-drillrow sc2-wide" style="'+rowStyle+'" data-dynpath="'+esc(node.path)+'"'
    +(node.leaf?'':' data-dyntoggle="1"')+'>'+name+drillCells(node.agg,node.path)+'</div>';
}
function sc2DynTreeRows(nodes,depth){
  var out='';
  nodes.forEach(function(n){
    out+=sc2DynNodeRow(n,depth);
    if(!n.leaf&&sc2DynExpanded[n.path])out+=sc2DynTreeRows(n.children,depth+1);
  });
  return out;
}
// Flex TGT total for the footer. Each visible top-level row shows its entered flex (if
// any) else its target; the hidden '—' bucket (product/item drills) and untouched rows
// keep their target. So total flex = total target − Σ(target − flex) over overridden
// top-level rows, and the total Dent = that Σ. Bal then tracks the flex total.
function sc2DynFlexTotal(totTarget){
  if(!sc2FlexOn())return totTarget;   // Flex off: Bal uses the real target, not saved flex values
  var dent=0;
  for(var i=0;i<sc2DynTree.length;i++){
    var n=sc2DynTree[i];
    if(sc2FlexStore.hasOwnProperty(n.path))dent+=(Number(n.agg.target)||0)-(Number(sc2FlexStore[n.path])||0);
  }
  return totTarget-dent;
}
// TOTAL-row cells: like drillCells but the Flex TGT is a read-only computed sum (not an
// editable scratch input), and Dent / Bal track it so the footer matches the column.
function drillCellsTotal(tot){
  var tgt=Number(tot.target)||0, flexSum=sc2DynFlexTotal(tgt);
  var dent=tgt-flexSum, dCls=dent>0?'sc2-dent-pos':(dent<0?'sc2-dent-neg':'');
  var bal=flexSum-((tot.done||0)+(tot.oih||0)), balClass=bal>=0?'sc2-bal-good':'sc2-bal-bad';
  var oihRlz=(tot.oih||0)>0?((tot.oihLineTotal||0)/tot.oih):NaN;
  var oihRlzStr=isFinite(oihRlz)?'₹'+fNp(oihRlz,2):'&mdash;';
  // Bal Realise measured against the FLEX target, so it tracks the Bal above it.
  var balRlz=(flexSum>0&&bal!==0)?((flexSum*(tot.targetRealise||0))-(tot.lineTotal||0))/bal:NaN;
  var balRlzStr=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';
  var balWo=flexSum-(tot.done||0), balWoClass=balWo>=0?'sc2-bal-good':'sc2-bal-bad';
  var tgtRlz=Number(tot.targetRealise)||0, actRlz=Number(tot.actualRealise)||0;
  return '<div class="sc2-drillval">'+fN(tgt)+'</div>'
    +(sc2FlexOn()?'<div class="sc2-drillval sc2-flexcol sc2-flextot">'+fN(flexSum)+'</div>'
        +'<div class="sc2-drillval sc2-flexcol cdent '+dCls+'">'+(dent!==0?fN(dent):'&mdash;')+'</div>':'')
    +'<div class="sc2-drillval">₹'+fNp(tgtRlz,2)+'</div>'
    +'<div class="sc2-drillval">'+fN(tot.done||0)+'</div>'
    +'<div class="sc2-drillval">₹'+fNp(actRlz,2)+'</div>'
    +'<div class="sc2-drillval">'+fN(tot.oih||0)+'</div>'
    +'<div class="sc2-drillval">'+oihRlzStr+'</div>'
    +'<div class="sc2-drillval sc2-bal-cell '+balClass+'">'+fN(bal)+'</div>'
    +'<div class="sc2-drillval sc2-balwo-cell '+balWoClass+'">'+fN(balWo)+'</div>'
    +'<div class="sc2-drillval sc2-balrlz-cell">'+balRlzStr+'</div>';
}
// Refresh just the footer's Flex/Dent/Bal in place after a per-row Flex TGT edit.
function sc2DynRefreshTotal(){
  if(!sc2DynTotalAgg)return;
  var totRow=document.querySelector('#channelGrid .sc2-drillrow.sc2-state-total');
  if(!totRow)return;
  var tgt=Number(sc2DynTotalAgg.target)||0, flexSum=sc2DynFlexTotal(tgt);
  var ft=totRow.querySelector('.sc2-flextot'); if(ft)ft.innerHTML=fN(flexSum);
  var dc=totRow.querySelector('.cdent');
  if(dc){var d=tgt-flexSum;dc.innerHTML=(d!==0?fN(d):'&mdash;');dc.className='sc2-drillval sc2-flexcol cdent '+(d>0?'sc2-dent-pos':(d<0?'sc2-dent-neg':''));}
  var bc=totRow.querySelector('.sc2-bal-cell');
  if(bc){var b=flexSum-((sc2DynTotalAgg.done||0)+(sc2DynTotalAgg.oih||0));bc.innerHTML=fN(b);bc.className='sc2-drillval sc2-bal-cell '+(b>=0?'sc2-bal-good':'sc2-bal-bad');}
  var bwc=totRow.querySelector('.sc2-balwo-cell');
  if(bwc){var bw=flexSum-(sc2DynTotalAgg.done||0);bwc.innerHTML=fN(bw);bwc.className='sc2-drillval sc2-balwo-cell '+(bw>=0?'sc2-bal-good':'sc2-bal-bad');}
}
function renderSc2DynTree(leaves){
  var dims=sc2DynOrder.slice();
  sc2DynNodeByPath={};
  sc2DynTree=dims.length?sc2DynBuildTree(leaves,dims,0,'',{}):[];
  var title=dims.length?dims.map(function(d){return dimMeta(d).label;}).join(' › '):'—';
  var html='<div class="sc2-drillcard sc2-drillfull">'
    +'<div class="sc2-thead sc2-wide" style="'+sc2DynGcols()+'">'
      +'<div class="sc2-tt-col" style="text-align:left">'+esc(title)+'</div>'
      +'<div class="sc2-tt-col">Target L</div>'+sc2FlexHeadCols()
      +'<div class="sc2-tt-col">Tgt Realise</div>'
      +'<div class="sc2-tt-col">Done L</div>'
      +'<div class="sc2-tt-col">Done Realise</div>'
      +'<div class="sc2-tt-col">Order in Hand</div>'
      +'<div class="sc2-tt-col">OIH Realise</div>'
      +'<div class="sc2-tt-col">Bal</div>'
      +'<div class="sc2-tt-col">Bal w/o OIH</div>'
      +'<div class="sc2-tt-col">Bal Realise</div>'
    +'</div><div class="sc2-drillbody">';
  if(!dims.length){
    html+='<div class="sc2-state-empty">Select at least one drill dimension.</div>';
  }else if(!sc2DynTree.length){
    html+='<div class="sc2-state-empty">No rows available in the current selection.</div>';
  }else{
    html+=sc2DynTreeRows(sc2DynTree,0);
    var tot=aggList(leaves); sc2DynTotalAgg=tot;
    html+='<div class="sc2-drillrow sc2-wide sc2-state-total" style="'+sc2DynGcols()+'"><div class="sc2-drillname">TOTAL</div>'+drillCellsTotal(tot)+'</div>';
  }
  html+='</div></div>';
  return html;
}
function sc2DynRerender(){
  var grid=document.getElementById('channelGrid');
  if(grid&&sc2DynLastLeaves){ grid.innerHTML=renderSc2DynTree(sc2DynLastLeaves); sc2DynFitHeight(); }
}
// Size the table body to the real space left below it so it uses the whole slide
// (more rows visible, no dead strip) while still shrinking to content when short
// (no white gap). max-height only caps — it never force-fills — so both hold.
function sc2DynFitHeight(){
  var grid=document.getElementById('channelGrid');
  if(!grid)return;
  var body=grid.querySelector('.sc2-drillfull .sc2-drillbody');
  if(!body)return;
  var top=body.getBoundingClientRect().top;
  var avail=window.innerHeight-top-16;   // 16px breathing room at the bottom
  body.style.maxHeight=(avail>140?avail:140)+'px';
}
var sc2DynResizeT;
window.addEventListener('resize',function(){
  clearTimeout(sc2DynResizeT);
  sc2DynResizeT=setTimeout(function(){
    var grid=document.getElementById('channelGrid');
    if(grid&&grid.querySelector('.sc2-drillfull'))sc2DynFitHeight();
  },150);
});
function sc2DynCsvRows(tree,total){
  var out=[['Drill: '+sc2DynOrder.map(function(k){return dimMeta(k).label;}).join(' > ')].concat(SC2_CSV_HEADER_TAIL)];
  (function walk(nodes,prefix){
    nodes.forEach(function(n){
      out.push(csvMetricRow(prefix+sc2DynLabel(n.label),n.agg));
      if(!n.leaf)walk(n.children,prefix+'    ');
    });
  })(tree,'');
  out.push(csvMetricRow('TOTAL',total));
  return out;
}
// ── Dynamic drill-order panel (replaces the old Main Group dropdown) ──
function sc2DynUpdateLabel(){
  var el=document.getElementById('sc2DynLabel');
  if(el)el.textContent=sc2DynOrder.length?sc2DynOrder.map(function(k){return dimMeta(k).label;}).join(' › '):'— none —';
}
function sc2DynRenderList(){
  var list=document.getElementById('sc2DynList'); if(!list)return;
  list.innerHTML='';
  SC2_DYN_DIMS.forEach(function(d){
    var pos=sc2DynChosen.indexOf(d.key);
    var item=document.createElement('label');
    item.className='com-dp-item'+(pos!==-1?' checked':'');
    item.innerHTML='<input type="checkbox" '+(pos!==-1?'checked':'')+'>'
      +'<span class="com-dp-name">'+d.name+'</span>'
      +'<span class="com-pos">'+(pos!==-1?pos+1:'')+'</span>';
    item.querySelector('input').addEventListener('change',function(){
      var i=sc2DynChosen.indexOf(d.key);
      if(this.checked){ if(i===-1)sc2DynChosen.push(d.key); }
      else if(i!==-1)sc2DynChosen.splice(i,1);
      sc2DynRenderList();
    });
    list.appendChild(item);
  });
}
function sc2DynInitPanel(){
  var btn=document.getElementById('sc2DynBtn'), panel=document.getElementById('sc2DynPanel');
  if(!btn||!panel)return;
  sc2DynUpdateLabel();
  btn.addEventListener('click',function(e){ e.stopPropagation(); sc2DynChosen=sc2DynOrder.slice(); sc2DynRenderList(); panel.classList.toggle('open'); });
  panel.addEventListener('click',function(e){ e.stopPropagation(); });
  document.addEventListener('click',function(){ panel.classList.remove('open'); });
  document.getElementById('sc2DynSelAll').addEventListener('click',function(){ sc2DynChosen=SC2_DYN_DIMS.map(function(d){return d.key;}); sc2DynRenderList(); });
  document.getElementById('sc2DynClrAll').addEventListener('click',function(){ sc2DynChosen=[]; sc2DynRenderList(); });
  document.getElementById('sc2DynApply').addEventListener('click',function(){
    if(!sc2DynChosen.length){ showToast('Pick at least one drill dimension','info'); return; }
    sc2DynOrder=sc2DynChosen.slice(); sc2DynExpanded={}; sc2DynUserSet=true;
    sc2DynUpdateLabel(); panel.classList.remove('open');
    renderSlideTwo();
  });
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',sc2DynInitPanel);
else sc2DynInitPanel();
// Expand / collapse dynamic-tree parent rows (delegated; survives re-renders).
document.addEventListener('click',function(e){
  if(!e.target||!e.target.closest)return;
  if(e.target.closest('.sc2-flexcol'))return;   // editing Flex TGT, not expand
  var row=e.target.closest('#channelGrid .sc2-drillrow[data-dyntoggle]');
  if(!row)return;
  var p=row.getAttribute('data-dynpath');
  sc2DynExpanded[p]=!sc2DynExpanded[p];
  sc2DynRerender();
});
// ── Persisted Flex TGT ──────────────────────────────────────────────────────
// Saved per segment + period (month/year) + drill row_key, so a typed value survives a
// page refresh. Loaded into sc2FlexStore before the drill table renders; auto-saved
// (debounced) on edit — no lock button.
async function loadFlexTargets(month,year){
  try{
    var seg=sc2Seg();
    var res=await fetch(API+'/api/flex-targets/?seg='+encodeURIComponent(seg)+'&month='+month+'&year='+year,{headers:{'X-CSRFToken':getCSRF()}});
    var p=res.ok?await res.json():{};
    var map=(p&&p.data)||{};
    for(var k in sc2FlexStore)delete sc2FlexStore[k];     // server is the source of truth
    for(var rk in map)sc2FlexStore[rk]=Number(map[rk])||0;
  }catch(e){ /* keep current sc2FlexStore on error */ }
}
var _flexSaveTimers={};
function saveFlexTarget(rowKey,value){
  var period=getResolvedChannelPeriod(), seg=sc2Seg();
  clearTimeout(_flexSaveTimers[rowKey]);
  _flexSaveTimers[rowKey]=setTimeout(function(){
    fetch(API+'/api/flex-targets/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},
      body:JSON.stringify({seg:seg,month:period.month,year:period.year,row_key:rowKey,value:value})
    }).catch(function(){});
  },450);
}
// Flexible View (slide-2 grids): live Dent update as Flex TGT is typed. In-place (no
// re-render) so the input keeps focus; value stored per row-path in sc2FlexStore.
document.getElementById('channelGrid').addEventListener('input',function(e){
  var inp=e.target&&e.target.closest?e.target.closest('input.sc2-flex-input'):null;if(!inp)return;
  var row=inp.closest('.sc2-drillrow');if(!row)return;
  var key=inp.getAttribute('data-flexkey'),raw=String(inp.value).trim();
  var tgt=Number(inp.getAttribute('data-tgt'))||0;
  var done=Number(inp.getAttribute('data-done'))||0,oih=Number(inp.getAttribute('data-oih'))||0;
  var trate=Number(inp.getAttribute('data-tr'))||0,lt=Number(inp.getAttribute('data-lt'))||0;
  var cleared=(raw===''||isNaN(Number(raw))),fv=cleared?null:Number(raw);
  if(cleared)delete sc2FlexStore[key]; else sc2FlexStore[key]=fv;
  if(row.hasAttribute('data-dynpath'))saveFlexTarget(key,cleared?null:fv);   // persist (auto-save, no button)
  // Dent = Target − Flex TGT.
  var dent=row.querySelector('.cdent');
  if(dent){
    if(cleared){dent.innerHTML='&mdash;';dent.className='sc2-drillval sc2-flexcol cdent';}
    else{var diff=tgt-fv;dent.innerHTML=fN(diff);dent.className='sc2-drillval sc2-flexcol cdent '+(diff>0?'sc2-dent-pos':(diff<0?'sc2-dent-neg':''));}
  }
  // Bal (and commodity's Bal Realise) recomputed on the effective target — flex if set, else original.
  var eff=cleared?tgt:fv, bal=eff-(done+oih);
  var balCell=row.querySelector('.sc2-bal-cell');
  if(balCell){balCell.innerHTML=fN(bal);balCell.className='sc2-drillval sc2-bal-cell '+(bal>=0?'sc2-bal-good':'sc2-bal-bad');}
  var balwoCell=row.querySelector('.sc2-balwo-cell');
  if(balwoCell){var bw=eff-done;balwoCell.innerHTML=fN(bw);balwoCell.className='sc2-drillval sc2-balwo-cell '+(bw>=0?'sc2-bal-good':'sc2-bal-bad');}
  var brCell=row.querySelector('.sc2-balrlz-cell');
  if(brCell){var balRlz=(eff>0&&bal!==0)?((eff*trate)-lt)/bal:NaN;brCell.innerHTML=isFinite(balRlz)?'₹'+fNp(balRlz,2):'&mdash;';}
  // Keep the TOTAL row's Flex / Dent / Bal in step with the edited row.
  if(row.hasAttribute('data-dynpath'))sc2DynRefreshTotal();
  else if(row.hasAttribute('data-compath'))comRefreshTotal();
});

/* ===== CHANNEL DASHBOARD (slide 2) PLAIN-CSV EXPORT ===== */
function csvInt(n){n=Number(n);return isFinite(n)?String(Math.round(n)):'0';}
function csvDec(n){n=Number(n);return isFinite(n)?String(Math.round(n*100)/100):'0';}
function csvBal(c){return (Number(c.target)||0)-((Number(c.done)||0)+(Number(c.oih)||0));}
function csvMetricRow(label,c){
  return [label,csvInt(c.target),csvInt(c.done),csvInt(c.oih),csvInt(csvBal(c)),
    csvDec(c.targetRealise),csvDec(c.actualRealise)];
}
var SC2_CSV_HEADER_TAIL=['Target Ltr','Done Ltr','OIH','Bal Ltr','Target Realise','Done Realise'];
function buildChannelCsvRows(layoutNames,crByName,agg,actualRealise){
  var out=[['Channel','Area'].concat(SC2_CSV_HEADER_TAIL)];
  var showAll=!!sc2Seg();
  for(var i=0;i<layoutNames.length;i++){
    var name=layoutNames[i], info=crByName[name];
    if(!info)continue;
    var visible=info.rows.filter(function(c){return showAll||(c.done||0)>0||(c.oih||0)>0;});
    for(var j=0;j<visible.length;j++)out.push([name].concat(csvMetricRow(visible[j].name,visible[j])));
    out.push([name].concat(csvMetricRow('TOTAL',info.total)));
  }
  out.push(['GRAND TOTAL',''].concat(csvMetricRow('',{
    target:agg.target,done:agg.done,oih:agg.oih,lineTotal:agg.lineTotal,
    targetRealise:agg.trW>0?agg.trWsum/agg.trW:0,actualRealise:actualRealise
  }).slice(1)));
  return out;
}
function buildDrillCsvRows(drillRows,total,mode,groupFilter){
  var dimLabel=(mode==='person'?'Sales Person':'State')+(groupFilter?(' ('+groupFilter+')'):'');
  var out=[[dimLabel].concat(SC2_CSV_HEADER_TAIL)];
  for(var i=0;i<drillRows.length;i++)out.push(csvMetricRow(drillRows[i].name,drillRows[i]));
  out.push(csvMetricRow('TOTAL',total));
  return out;
}
function csvEscapeCell(v){v=String(v==null?'':v);return /[",\r\n]/.test(v)?('"'+v.replace(/"/g,'""')+'"'):v;}
// Raw data behind the whole main view — the underlying SAP sales rows (segment-filtered),
// dumped as-is to CSV (opens in Excel). Header = union of row keys.
function exportMainRawCSV(){
  if(!sc2Fetched){showToast('Fetch the channel dashboard first','info');return;}
  var rows=getFilteredChannelRows();
  if(!rows.length){showToast('Nothing to export','info');return;}
  var cols=[],seen={};
  for(var i=0;i<rows.length;i++)for(var k in rows[i])if(!seen[k]){seen[k]=1;cols.push(k);}
  var lines=[cols.map(csvEscapeCell).join(',')];
  for(var r=0;r<rows.length;r++)lines.push(cols.map(function(c){return csvEscapeCell(rows[r][c]);}).join(','));
  var from=document.getElementById('sc2From').value,to=document.getElementById('sc2To').value;
  var blob=new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/csv;charset=utf-8;'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download='Sales_Channel_raw_'+from+'_'+to+'.csv';a.click();
  setTimeout(function(){URL.revokeObjectURL(url);},1000);
  showToast('Raw CSV exported','ok');
}
function exportChannelCSV(){
  if(!sc2Fetched){showToast('Fetch the channel dashboard first','info');return;}
  if(!sc2CsvRows||!sc2CsvRows.length){showToast('Nothing to export','info');return;}
  var text=sc2CsvRows.map(function(r){return r.map(csvEscapeCell).join(',');}).join('\r\n');
  var from=document.getElementById('sc2From').value, to=document.getElementById('sc2To').value;
  var blob=new Blob(['\uFEFF'+text],{type:'text/csv;charset=utf-8;'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download='Sales_Channel_'+from+'_'+to+'.csv';a.click();
  setTimeout(function(){URL.revokeObjectURL(url);},1000);
  showToast('CSV exported','ok');
}

async function exportCSV(){
  if(!dataFetched){showToast('Fetch data first','info');return;}
  try{
    var res=await fetch(API+'/api/export-excel/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},
      body:JSON.stringify({layout_rows:buildExcelLayoutRows('Realise Dashboard')})
    });
    if(!res.ok){var err='Export failed';try{var ej=await res.json();err=ej.detail||ej.error||err;}catch(_){}throw new Error(err);}
    var blob=await res.blob();
    var cd=res.headers.get('Content-Disposition')||'';
    var name='Realise_Export_'+document.getElementById('dFrom').value+'_'+document.getElementById('dTo').value+'.xlsx';
    var m=/filename="([^"]+)"/i.exec(cd);if(m&&m[1])name=m[1];
    var url=URL.createObjectURL(blob);
    var a=document.createElement('a');a.href=url;a.download=name;a.click();
    setTimeout(function(){URL.revokeObjectURL(url);},1000);
    showToast('Excel export downloaded','ok');
  }catch(e){showToast(e.message||'Export failed','err');}
}

function buildExcelLayoutRows(title){
  var rows=[
    [{value:title,style:1,colspan:12}],
    [{value:'From',style:1},{value:document.getElementById('dFrom').value},{value:'To',style:1},{value:document.getElementById('dTo').value}],
    []
  ];
  document.querySelectorAll('.summary .scard').forEach(function(card){
    rows.push([{value:card.querySelector('.sl').innerText.trim(),style:1},{value:card.querySelector('.sv').innerText.trim()}]);
  });
  rows.push([]);
  var table=document.getElementById('tbl');
  table.querySelectorAll('tr').forEach(function(tr){
    var style=tr.classList.contains('grp-row')?2:(tr.closest('thead')?1:0);
    var cells=[];
    tr.querySelectorAll('th,td').forEach(function(cell){
      cells.push({value:cell.innerText.trim(),colspan:cell.colSpan||1,style:style});
    });
    rows.push(cells);
  });
  return rows;
}

function fN(n,d){d=d||0;if(n===null||n===undefined||isNaN(n))return'&mdash;';return new Intl.NumberFormat('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}).format(n);}
function fNp(n,d){d=d||0;if(n===null||n===undefined||isNaN(n))return'—';return new Intl.NumberFormat('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}).format(n);}
/* Rupees in Indian short form, so a big revenue figure fits inside a KPI card:
   4,43,26,000 -> "₹4.43 Cr". "Lakh" is spelled out on purpose - a bare "L" next to
   a litres figure reads as litres. */
function fRsShort(n){
  n=Number(n)||0;
  var sign=n<0?'-':'', a=Math.abs(n);
  if(a>=1e7)return sign+'₹'+fNp(a/1e7,2)+' Cr';
  if(a>=1e5)return sign+'₹'+fNp(a/1e5,2)+' Lakh';
  return sign+'₹'+fNp(a,0);
}
function fC(n){if(n===null||n===undefined||isNaN(n))return'&mdash;';return'₹'+new Intl.NumberFormat('en-IN',{maximumFractionDigits:0}).format(Math.round(n));}
function vc(v){return v>0?'v-pos':v<0?'v-neg':'v-zero';}
function showToast(msg,type){var el=document.getElementById('toast');el.textContent=msg;el.className='toast toast-'+(type||'info')+' show';setTimeout(function(){el.classList.remove('show');},3500);}

/* ===== UPDATE TARGETS ===== */
function openUpdateTargetsModal(){
  if(!currentUser||!currentUser.can_edit){showToast('Only admin can update targets','err');return;}
  updateAuthUser=null;
  document.getElementById('adminReauthModal').classList.add('show');
  document.getElementById('reauthError').style.display='none';
  document.getElementById('reauthPass').value='';
  setTimeout(function(){document.getElementById('reauthPass').focus();},100);
}
function closeReauthModal(){document.getElementById('adminReauthModal').classList.remove('show');}
function showReauthError(msg){var el=document.getElementById('reauthError');el.textContent=msg;el.style.display='block';}
async function submitReauth(){
  var p=document.getElementById('reauthPass').value;
  if(!p){showReauthError('Enter password');return;}
  try{
    var res=await fetch(API+'/api/verify-pin/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify({pin:p})});
    var result=await res.json();
    if(result.status==='ok'&&result.verified){updateAuthUser=currentUser;closeReauthModal();openTargetsTableModal();return;}
    showReauthError('Incorrect password');
  }catch(e){showReauthError('Verification failed');}
}
async function openTargetsTableModal(){
  var btn=document.querySelector('#updateTargetsModal .btn-confirm');
  btn.disabled=false;btn.textContent='Save All';
  var mSel=document.getElementById('utMonth');
  var yInp=document.getElementById('utYear');
  mSel.innerHTML='';
  for(var i=0;i<MONTHS.length;i++){var opt=document.createElement('option');opt.value=i+1;opt.textContent=MONTHS[i];mSel.appendChild(opt);}
  var mIdx=MONTHS.indexOf(document.getElementById('fMonth').value);
  mSel.value=(mIdx>=0?(mIdx+1):(new Date().getMonth()+1));
  var curYear=parseInt(document.getElementById('fYear').value)||new Date().getFullYear();
  yInp.innerHTML='';
  for(var y=curYear+1;y>=curYear-4;y--){var yo=document.createElement('option');yo.value=y;yo.textContent=y;yInp.appendChild(yo);}
  yInp.value=curYear;
  document.getElementById('updateTargetsModal').classList.add('show');
  await loadTargetsIntoTable();
}
function closeUpdateTargetsModal(){document.getElementById('updateTargetsModal').classList.remove('show');}
var utSortedProducts=[];
async function loadTargetsIntoTable(){
  var m=parseInt(document.getElementById('utMonth').value||0,10);
  var y=parseInt(document.getElementById('utYear').value||0,10);
  if(!m||!y)return;
  var data={};
  try{
    var res=await fetch(API+'/api/targets/?month='+m+'&year='+y,{headers:{'X-CSRFToken':getCSRF()}});
    if(res.ok){var payload=await res.json();data=payload.data||{};}
  }catch(e){}
  utSortedProducts=PRODUCTS.slice().sort(function(a,b){
    var order={'PREMIUM':0,'COMMODITY':1};
    var ta=order[a.u_type]!=null?order[a.u_type]:2;
    var tb=order[b.u_type]!=null?order[b.u_type]:2;
    if(ta!==tb)return ta-tb;
    var ka=a.u_type+'|'+a.u_sub_group,kb=b.u_type+'|'+b.u_sub_group;
    var la=(data[ka]&&data[ka].tgt_ltrs!=null)?data[ka].tgt_ltrs:a.ts;
    var lb=(data[kb]&&data[kb].tgt_ltrs!=null)?data[kb].tgt_ltrs:b.ts;
    return lb-la;
  });
  var tbody=document.getElementById('utTableBody');tbody.innerHTML='';
  var lastType='';
  for(var p=0;p<utSortedProducts.length;p++){
    var pr=utSortedProducts[p];
    if(pr.u_type!==lastType){lastType=pr.u_type;var gtr=document.createElement('tr');gtr.className='ut-group-row';gtr.innerHTML='<td colspan="4">'+pr.u_type+'</td>';tbody.appendChild(gtr);}
    var key=pr.u_type+'|'+pr.u_sub_group;
    var saved=data[key]||{};
    var ltrs=saved.tgt_ltrs!=null?saved.tgt_ltrs:pr.ts;
    var rate=saved.tgt_rate!=null?saved.tgt_rate:pr.tr;
    var tr=document.createElement('tr');
    tr.innerHTML='<td></td><td>'+pr.u_sub_group+'</td>'
      +'<td><input type="number" data-idx="'+p+'" data-field="ltrs" value="'+ltrs+'"></td>'
      +'<td><input type="number" data-idx="'+p+'" data-field="rate" value="'+rate+'"></td>';
    tbody.appendChild(tr);
  }
}
async function saveAllTargets(){
  var btn=document.querySelector('#updateTargetsModal .btn-confirm');
  var origText=btn.textContent;
  btn.disabled=true;btn.textContent='Saving…';
  showToast('Saving targets…','info');
  var m=parseInt(document.getElementById('utMonth').value,10);
  var y=parseInt(document.getElementById('utYear').value,10);
  var dataRows=document.querySelectorAll('#utTableBody tr:not(.ut-group-row)');
  var targets=[];
  for(var i=0;i<dataRows.length;i++){
    var pr=utSortedProducts[i];
    var ltrs=parseFloat(dataRows[i].querySelector('[data-field="ltrs"]').value)||0;
    var rate=parseFloat(dataRows[i].querySelector('[data-field="rate"]').value)||0;
    targets.push({key:pr.u_type+'|'+pr.u_sub_group,tgt_ltrs:ltrs,tgt_rate:rate});
  }
  try{
    var res=await fetch(API+'/api/save-targets/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCSRF()},body:JSON.stringify({month:m,year:y,targets:targets})});
    if(!res.ok){var err=await res.json();throw new Error(err.detail||err.error||'Save failed');}
    showToast('All targets saved for '+MONTHS[m-1]+' '+y,'ok');
    await loadSavedTargets();
    closeUpdateTargetsModal();
    await applyFilters();
  }catch(e){
    showToast('Save failed: '+e.message,'err');
    btn.disabled=false;btn.textContent=origText;
  }
}

/* ===== PRODUCT FILTER ===== */
function buildProductFilterList(){
  var html='';
  var sorted=PRODUCTS.slice().sort(function(a,b){return b.u_type.localeCompare(a.u_type)||a.u_sub_group.localeCompare(b.u_sub_group);});
  for(var i=0;i<sorted.length;i++){
    var p=sorted[i],pk=p.u_type+'|'+p.u_sub_group,checked=selectedProducts[pk]!==false;
    var bc=p.u_type==='PREMIUM'?'badge-prem':'badge-comm';
    html+='<div class="pf-item"><input type="checkbox" id="pf_'+i+'" '+(checked?'checked':'')+' data-prod="'+pk+'"><label for="pf_'+i+'"><span class="pf-badge '+bc+'">'+p.u_type+'</span> '+p.u_sub_group+'</label></div>';
  }
  document.getElementById('pfList').innerHTML=html;
}
function toggleProductFilter(){var drop=document.getElementById('pfDrop');if(drop.classList.contains('show')){drop.classList.remove('show');return;}buildProductFilterList();drop.classList.add('show');}
function pfSelectAll(){var cbs=document.querySelectorAll('#pfList input[type=checkbox]');for(var i=0;i<cbs.length;i++)cbs[i].checked=true;}
function pfClearAll(){var cbs=document.querySelectorAll('#pfList input[type=checkbox]');for(var i=0;i<cbs.length;i++)cbs[i].checked=false;}
function applyProductFilter(){
  var cbs=document.querySelectorAll('#pfList input[type=checkbox]');var count=0;
  for(var i=0;i<cbs.length;i++){selectedProducts[cbs[i].dataset.prod]=cbs[i].checked;if(cbs[i].checked)count++;}
  document.getElementById('pfDrop').classList.remove('show');
  var btn=document.getElementById('pfBtn');
  if(count===PRODUCTS.length){btn.innerHTML='All Products <span class="pf-count" id="pfCount" style="display:none"></span> &#9662;';}
  else{btn.innerHTML=count+' Products <span class="pf-count" id="pfCount">'+count+'/'+PRODUCTS.length+'</span> &#9662;';}
  applyFilters();
}
document.addEventListener('click',function(e){var wrap=document.querySelector('.prod-filter-wrap');if(wrap&&!wrap.contains(e.target)){document.getElementById('pfDrop').classList.remove('show');}});

/* ===== ROLE RESTRICTIONS (applied on DOMContentLoaded) ===== */
function applyRoleRestrictions(){
  if(!currentUser)return;
  var u=currentUser;
  var fType=document.getElementById('fType');
  if(u.type_filter){fType.value=u.type_filter;fType.disabled=true;fType.style.opacity='0.6';}
  else{fType.value='';fType.disabled=false;fType.style.opacity='1';}
  if(u.can_edit){document.getElementById('updateTargetsBtn').style.display='';}
  else{document.getElementById('updateTargetsBtn').style.display='none';}
  var slideTwoTargetLink=document.getElementById('sc2TargetsLink');
  if(slideTwoTargetLink)slideTwoTargetLink.style.display=u.can_edit?'':'none';
}