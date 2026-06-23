const {
  useState,
  useMemo,
  useCallback,
  useEffect,
  useRef
} = React;

/* ─────────────────────────────────────────────────────────────────────────
   SEED — initial mapping from expanded_main_group_state_mapping.xlsx.
   This is just the STARTING data. Everything below is fully dynamic — add or
   remove salespeople / states / channels in the "Manage / Add" tab and all
   five visuals update + save to the browser automatically.
   ───────────────────────────────────────────────────────────────────────── */
/* Live data loads from the Django backend (territory-map + territory-product-targets
   APIs). SEED stays empty — only a pre-load placeholder. */
const SEED = [];

/* Known channels get curated colors; any NEW channel gets an auto colour. */
const KNOWN = {
  GT: {
    full: 'General Trade',
    color: '#4f46e5'
  },
  MT: {
    full: 'Modern Trade',
    color: '#10b981'
  },
  ROI: {
    full: 'Rest of India',
    color: '#f59e0b'
  },
  HORECA: {
    full: 'Hotel · Restaurant · Café',
    color: '#ec4899'
  }
};
const KNOWN_ORDER = ['GT', 'MT', 'ROI', 'HORECA'];
const AV_COLORS = ['#4f46e5', '#0891b2', '#7c3aed', '#db2777', '#ea580c', '#0d9488', '#9333ea', '#2563eb', '#c026d3'];

/* Full India list — 28 states + 8 union territories. The 6 PRIORITY states show
   inline; everything else lives behind the "More" popup. */
const INDIA_STATES = ['ANDHRA PRADESH', 'ARUNACHAL PRADESH', 'ASSAM', 'BIHAR', 'CHHATTISGARH', 'GOA', 'GUJARAT', 'HARYANA', 'HIMACHAL PRADESH', 'JHARKHAND', 'KARNATAKA', 'KERALA', 'MADHYA PRADESH', 'MAHARASHTRA', 'MANIPUR', 'MEGHALAYA', 'MIZORAM', 'NAGALAND', 'ODISHA', 'PUNJAB', 'RAJASTHAN', 'SIKKIM', 'TAMIL NADU', 'TELANGANA', 'TRIPURA', 'UTTAR PRADESH', 'UTTARAKHAND', 'WEST BENGAL'];
const INDIA_UTS = ['ANDAMAN AND NICOBAR ISLANDS', 'CHANDIGARH', 'DADRA AND NAGAR HAVELI AND DAMAN AND DIU', 'DELHI', 'JAMMU AND KASHMIR', 'LADAKH', 'LAKSHADWEEP', 'PUDUCHERRY'];
const PRIORITY_STATES = ['DELHI', 'PUNJAB', 'HARYANA', 'UTTAR PRADESH', 'RAJASTHAN', 'GOA'];

/* Full channel master. The 4 PRIORITY channels show inline; the rest live in the
   "More channels" popup. */
/* The 7 dashboard DISPLAY channels (match the grid + Sales Channel Dashboard). */
const ALL_CHANNELS = ['GT', 'MT', 'ROI', 'ECOM', 'HORECA', 'CSD', 'REST'];
const PRIORITY_CHANNELS = ['GT', 'MT', 'ROI', 'ECOM'];

/* District master per state (standard lists; a "+ Add custom" fallback covers any
   gaps/renames). The SO "City / District" step shows the list for the chosen state;
   states not in this map fall back to a free-text box. */
const DISTRICTS = {
  'DELHI': ['NEW DELHI', 'CENTRAL DELHI', 'NORTH DELHI', 'SOUTH DELHI', 'EAST DELHI', 'WEST DELHI', 'NORTH EAST DELHI', 'NORTH WEST DELHI', 'SOUTH WEST DELHI', 'SOUTH EAST DELHI', 'SHAHDARA'],
  'PUNJAB': ['AMRITSAR', 'BARNALA', 'BATHINDA', 'FARIDKOT', 'FATEHGARH SAHIB', 'FAZILKA', 'FEROZEPUR', 'GURDASPUR', 'HOSHIARPUR', 'JALANDHAR', 'KAPURTHALA', 'LUDHIANA', 'MALERKOTLA', 'MANSA', 'MOGA', 'PATHANKOT', 'PATIALA', 'RUPNAGAR', 'MOHALI', 'SANGRUR', 'NAWANSHAHR', 'SRI MUKTSAR SAHIB', 'TARN TARAN'],
  'HARYANA': ['AMBALA', 'BHIWANI', 'CHARKHI DADRI', 'FARIDABAD', 'FATEHABAD', 'GURUGRAM', 'HISAR', 'JHAJJAR', 'JIND', 'KAITHAL', 'KARNAL', 'KURUKSHETRA', 'MAHENDRAGARH', 'NUH', 'PALWAL', 'PANCHKULA', 'PANIPAT', 'REWARI', 'ROHTAK', 'SIRSA', 'SONIPAT', 'YAMUNANAGAR'],
  'UTTAR PRADESH': ['AGRA', 'ALIGARH', 'AMBEDKAR NAGAR', 'AMETHI', 'AMROHA', 'AURAIYA', 'AYODHYA', 'AZAMGARH', 'BAGHPAT', 'BAHRAICH', 'BALLIA', 'BALRAMPUR', 'BANDA', 'BARABANKI', 'BAREILLY', 'BASTI', 'BHADOHI', 'BIJNOR', 'BUDAUN', 'BULANDSHAHR', 'CHANDAULI', 'CHITRAKOOT', 'DEORIA', 'ETAH', 'ETAWAH', 'FARRUKHABAD', 'FATEHPUR', 'FIROZABAD', 'GAUTAM BUDDHA NAGAR', 'GHAZIABAD', 'GHAZIPUR', 'GONDA', 'GORAKHPUR', 'HAMIRPUR', 'HAPUR', 'HARDOI', 'HATHRAS', 'JALAUN', 'JAUNPUR', 'JHANSI', 'KANNAUJ', 'KANPUR DEHAT', 'KANPUR NAGAR', 'KASGANJ', 'KAUSHAMBI', 'LAKHIMPUR KHERI', 'KUSHINAGAR', 'LALITPUR', 'LUCKNOW', 'MAHARAJGANJ', 'MAHOBA', 'MAINPURI', 'MATHURA', 'MAU', 'MEERUT', 'MIRZAPUR', 'MORADABAD', 'MUZAFFARNAGAR', 'PILIBHIT', 'PRATAPGARH', 'PRAYAGRAJ', 'RAEBARELI', 'RAMPUR', 'SAHARANPUR', 'SAMBHAL', 'SANT KABIR NAGAR', 'SHAHJAHANPUR', 'SHAMLI', 'SHRAVASTI', 'SIDDHARTHNAGAR', 'SITAPUR', 'SONBHADRA', 'SULTANPUR', 'UNNAO', 'VARANASI'],
  'UTTARAKHAND': ['ALMORA', 'BAGESHWAR', 'CHAMOLI', 'CHAMPAWAT', 'DEHRADUN', 'HARIDWAR', 'NAINITAL', 'PAURI GARHWAL', 'PITHORAGARH', 'RUDRAPRAYAG', 'TEHRI GARHWAL', 'UDHAM SINGH NAGAR', 'UTTARKASHI'],
  'RAJASTHAN': ['AJMER', 'ALWAR', 'BANSWARA', 'BARAN', 'BARMER', 'BHARATPUR', 'BHILWARA', 'BIKANER', 'BUNDI', 'CHITTORGARH', 'CHURU', 'DAUSA', 'DHOLPUR', 'DUNGARPUR', 'HANUMANGARH', 'JAIPUR', 'JAISALMER', 'JALORE', 'JHALAWAR', 'JHUNJHUNU', 'JODHPUR', 'KARAULI', 'KOTA', 'NAGAUR', 'PALI', 'PRATAPGARH', 'RAJSAMAND', 'SAWAI MADHOPUR', 'SIKAR', 'SIROHI', 'SRI GANGANAGAR', 'TONK', 'UDAIPUR'],
  'GOA': ['NORTH GOA', 'SOUTH GOA'],
  'KARNATAKA': ['BAGALKOT', 'BALLARI', 'BELAGAVI', 'BENGALURU RURAL', 'BENGALURU URBAN', 'BIDAR', 'CHAMARAJANAGAR', 'CHIKKABALLAPUR', 'CHIKKAMAGALURU', 'CHITRADURGA', 'DAKSHINA KANNADA', 'DAVANAGERE', 'DHARWAD', 'GADAG', 'HASSAN', 'HAVERI', 'KALABURAGI', 'KODAGU', 'KOLAR', 'KOPPAL', 'MANDYA', 'MYSURU', 'RAICHUR', 'RAMANAGARA', 'SHIVAMOGGA', 'TUMAKURU', 'UDUPI', 'UTTARA KANNADA', 'VIJAYANAGARA', 'VIJAYAPURA', 'YADGIR'],
  'TELANGANA': ['ADILABAD', 'BHADRADRI KOTHAGUDEM', 'HANUMAKONDA', 'HYDERABAD', 'JAGTIAL', 'JANGAON', 'JAYASHANKAR BHUPALPALLY', 'JOGULAMBA GADWAL', 'KAMAREDDY', 'KARIMNAGAR', 'KHAMMAM', 'KOMARAM BHEEM', 'MAHABUBABAD', 'MAHABUBNAGAR', 'MANCHERIAL', 'MEDAK', 'MEDCHAL-MALKAJGIRI', 'MULUGU', 'NAGARKURNOOL', 'NALGONDA', 'NARAYANPET', 'NIRMAL', 'NIZAMABAD', 'PEDDAPALLI', 'RAJANNA SIRCILLA', 'RANGAREDDY', 'SANGAREDDY', 'SIDDIPET', 'SURYAPET', 'VIKARABAD', 'WANAPARTHY', 'WARANGAL', 'YADADRI BHUVANAGIRI'],
  'MAHARASHTRA': ['AHMEDNAGAR', 'AKOLA', 'AMRAVATI', 'CHHATRAPATI SAMBHAJINAGAR', 'BEED', 'BHANDARA', 'BULDHANA', 'CHANDRAPUR', 'DHULE', 'GADCHIROLI', 'GONDIA', 'HINGOLI', 'JALGAON', 'JALNA', 'KOLHAPUR', 'LATUR', 'MUMBAI CITY', 'MUMBAI SUBURBAN', 'NAGPUR', 'NANDED', 'NANDURBAR', 'NASHIK', 'DHARASHIV', 'PALGHAR', 'PARBHANI', 'PUNE', 'RAIGAD', 'RATNAGIRI', 'SANGLI', 'SATARA', 'SINDHUDURG', 'SOLAPUR', 'THANE', 'WARDHA', 'WASHIM', 'YAVATMAL'],
  'GUJARAT': ['AHMEDABAD', 'AMRELI', 'ANAND', 'ARAVALLI', 'BANASKANTHA', 'BHARUCH', 'BHAVNAGAR', 'BOTAD', 'CHHOTA UDEPUR', 'DAHOD', 'DANG', 'DEVBHOOMI DWARKA', 'GANDHINAGAR', 'GIR SOMNATH', 'JAMNAGAR', 'JUNAGADH', 'KHEDA', 'KUTCH', 'MAHISAGAR', 'MEHSANA', 'MORBI', 'NARMADA', 'NAVSARI', 'PANCHMAHAL', 'PATAN', 'PORBANDAR', 'RAJKOT', 'SABARKANTHA', 'SURAT', 'SURENDRANAGAR', 'TAPI', 'VADODARA', 'VALSAD'],
  'WEST BENGAL': ['ALIPURDUAR', 'BANKURA', 'BIRBHUM', 'COOCH BEHAR', 'DAKSHIN DINAJPUR', 'DARJEELING', 'HOOGHLY', 'HOWRAH', 'JALPAIGURI', 'JHARGRAM', 'KALIMPONG', 'KOLKATA', 'MALDA', 'MURSHIDABAD', 'NADIA', 'NORTH 24 PARGANAS', 'PASCHIM BARDHAMAN', 'PASCHIM MEDINIPUR', 'PURBA BARDHAMAN', 'PURBA MEDINIPUR', 'PURULIA', 'SOUTH 24 PARGANAS', 'UTTAR DINAJPUR'],
  'ASSAM': ['BAKSA', 'BARPETA', 'BISWANATH', 'BONGAIGAON', 'CACHAR', 'CHARAIDEO', 'CHIRANG', 'DARRANG', 'DHEMAJI', 'DHUBRI', 'DIBRUGARH', 'DIMA HASAO', 'GOALPARA', 'GOLAGHAT', 'HAILAKANDI', 'HOJAI', 'JORHAT', 'KAMRUP', 'KAMRUP METROPOLITAN', 'KARBI ANGLONG', 'KARIMGANJ', 'KOKRAJHAR', 'LAKHIMPUR', 'MAJULI', 'MORIGAON', 'NAGAON', 'NALBARI', 'SIVASAGAR', 'SONITPUR', 'SOUTH SALMARA-MANKACHAR', 'TINSUKIA', 'UDALGURI', 'WEST KARBI ANGLONG']
};

/* helpers */
const keyOf = (channel, state) => `${channel}||${state}`;
const fmt = n => '₹' + new Intl.NumberFormat('en-IN').format(Math.round(n || 0));
const pid = (type, name) => type + '#' + name; // product id, e.g. P#CANOLA
const _pl = x => x && typeof x === 'object' ? +x.l || 0 : +x || 0; // tgt litres of a product entry (legacy number = litres)
const _pr = x => x && typeof x === 'object' ? +x.r || 0 : 0; // tgt realise of a product entry
const tProdL = (v, id) => v && typeof v === 'object' ? _pl(v[id]) : 0; // target litres for one product
const tProdR = (v, id) => v && typeof v === 'object' ? _pr(v[id]) : 0; // target realise for one product
const tSum = v => v && typeof v === 'object' ? Object.values(v).reduce((s, x) => s + _pl(x), 0) : +v || 0; // total litres
const tByType = (v, type) => {
  // premium 'P' / commodity 'C' litres subtotal
  if (!v || typeof v !== 'object') return type === 'C' ? +v || 0 : 0; // legacy bare number = litres
  if ('p' in v || 'c' in v) return type === 'P' ? +v.p || 0 : +v.c || 0; // very old {p,c}
  let s = 0;
  for (const k in v) {
    if (k[0] === type && k[1] === '#') s += _pl(v[k]);
  }
  return s; // product map (litres)
};
const fmtL = n => new Intl.NumberFormat('en-IN').format(Math.round(n || 0)) + ' L'; // litres formatter
const fmtK = n => {
  n = Math.round(n || 0);
  return n >= 1000 ? Math.round(n / 1000) + 'k' : String(n);
}; // compact (771410 -> 771k)
const PRODUCT_SEED = [{
  name: 'CANOLA',
  type: 'P'
}, {
  name: 'OLIVE',
  type: 'P'
}, {
  name: 'GROUNDNUT',
  type: 'P'
}, {
  name: 'EXTRA VIRGIN OLIVE',
  type: 'P'
}, {
  name: 'GHEE',
  type: 'P'
}, {
  name: 'BLENDED',
  type: 'P'
}, {
  name: 'YELLOW MUSTARD',
  type: 'P'
}, {
  name: 'COCONUT',
  type: 'P'
}, {
  name: 'SESAME',
  type: 'P'
}, {
  name: 'SLICED OLIVE',
  type: 'P'
}, {
  name: 'MUSTARD',
  type: 'C'
}, {
  name: 'SOYABEAN',
  type: 'C'
}, {
  name: 'SUNFLOWER',
  type: 'C'
}, {
  name: 'BLENDED',
  type: 'C'
}, {
  name: 'RICE BRAN',
  type: 'C'
}, {
  name: 'COTTON SEED',
  type: 'C'
}];
const distinct = arr => [...new Set(arr)];
const norm = s => (s || '').trim().toUpperCase();
function hashColor(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = h * 31 + str.charCodeAt(i) >>> 0;
  return AV_COLORS[h % AV_COLORS.length];
}
function hexToRgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
}
function channelMeta(ch) {
  const k = KNOWN[ch];
  const color = k ? k.color : hashColor(ch);
  return {
    full: k ? k.full : ch,
    color,
    light: hexToRgba(color, 0.13)
  };
}
function orderChannels(rows) {
  const present = distinct(rows.map(r => r.channel));
  return [...KNOWN_ORDER.filter(c => present.includes(c)), ...present.filter(c => !KNOWN_ORDER.includes(c))];
}
const initials = name => name.replace(/ JI$/, '').split(' ').filter(Boolean).map(w => w[0]).slice(0, 2).join('');
function Avatar({
  name,
  size
}) {
  const s = size || 34;
  return /*#__PURE__*/React.createElement("div", {
    className: "avatar",
    style: {
      background: hashColor(name),
      width: s,
      height: s,
      fontSize: s * 0.37
    }
  }, initials(name));
}
function ChannelChip({
  ch
}) {
  const c = channelMeta(ch);
  return /*#__PURE__*/React.createElement("span", {
    className: "chip",
    style: {
      background: c.light,
      color: c.color
    }
  }, ch);
}
function TargetInput({
  value,
  onChange,
  width
}) {
  return /*#__PURE__*/React.createElement("span", {
    className: "tinput-wrap"
  }, /*#__PURE__*/React.createElement("span", {
    className: "pre"
  }, "\u20B9"), /*#__PURE__*/React.createElement("input", {
    className: "tinput",
    style: width ? {
      width
    } : null,
    type: "number",
    min: "0",
    step: "10000",
    placeholder: "0",
    value: value ?? '',
    onChange: e => onChange(e.target.value)
  }));
}
/* Click-to-select chips instead of a dropdown: all options visible, tap to pick,
   "+ New" reveals a tiny inline box to create a brand-new value. */
function ChipPicker({
  label,
  value,
  onChange,
  options,
  accentFor,
  icon,
  addLabel
}) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState('');
  const known = options.map(norm);
  const custom = norm(value) && !known.includes(norm(value));
  const commit = () => {
    const v = norm(draft);
    if (v) onChange(v);
    setDraft('');
    setAdding(false);
  };
  const onStyle = ac => ({
    borderColor: ac,
    background: hexToRgba(ac, .12),
    color: ac,
    boxShadow: '0 0 0 3px ' + hexToRgba(ac, .16)
  });
  return /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, label, " ", custom && /*#__PURE__*/React.createElement("span", {
    className: "new-badge"
  }, "\u2726 NEW")), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, options.map(o => {
    const ac = accentFor ? accentFor(o) : '#4f46e5';
    const on = norm(value) === norm(o);
    return /*#__PURE__*/React.createElement("button", {
      key: o,
      className: "pick",
      onClick: () => onChange(o),
      style: on ? onStyle(ac) : null
    }, icon && icon(o), o);
  }), custom && /*#__PURE__*/React.createElement("button", {
    className: "pick",
    onClick: () => onChange(''),
    title: "Click to clear",
    style: onStyle(accentFor ? accentFor(value) : '#16a34a')
  }, icon && icon(value), value, " \u2715"), adding ? /*#__PURE__*/React.createElement("input", {
    autoFocus: true,
    className: "pick-input",
    value: draft,
    placeholder: 'New ' + (addLabel || label) + '…',
    onChange: e => setDraft(e.target.value),
    onKeyDown: e => {
      if (e.key === 'Enter') commit();
      if (e.key === 'Escape') {
        setAdding(false);
        setDraft('');
      }
    },
    onBlur: commit
  }) : /*#__PURE__*/React.createElement("button", {
    className: "pick add",
    onClick: () => setAdding(true)
  }, "+ New")));
}

/* State picker: 6 key states inline + a "More" popup with every Indian state & UT. */
const PICK_ON = {
  borderColor: '#4f46e5',
  background: hexToRgba('#4f46e5', .12),
  color: '#4f46e5',
  boxShadow: '0 0 0 3px ' + hexToRgba('#4f46e5', .16)
};
function StatePicker({
  value,
  onChange
}) {
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState('');
  const [q, setQ] = useState('');
  const all = [...INDIA_STATES, ...INDIA_UTS];
  const custom = norm(value) && !all.map(norm).includes(norm(value));
  const selectedExtra = norm(value) && !PRIORITY_STATES.includes(norm(value)) && !custom;
  const moreStates = INDIA_STATES.filter(s => !PRIORITY_STATES.includes(s));
  const moreUTs = INDIA_UTS.filter(s => !PRIORITY_STATES.includes(s));
  const moreCount = moreStates.length + moreUTs.length;
  const commit = () => {
    const v = norm(draft);
    if (v) onChange(v);
    setDraft('');
    setAdding(false);
  };
  const pick = s => {
    onChange(s);
    setOpen(false);
    setQ('');
  };
  const greenOn = {
    borderColor: '#16a34a',
    background: hexToRgba('#16a34a', .12),
    color: '#16a34a',
    boxShadow: '0 0 0 3px ' + hexToRgba('#16a34a', .16)
  };
  useEffect(() => {
    if (!open) return;
    const h = e => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [open]);
  const f = s => s.includes(norm(q));
  const fStates = moreStates.filter(f),
    fUTs = moreUTs.filter(f);
  return /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "State ", custom && /*#__PURE__*/React.createElement("span", {
    className: "new-badge"
  }, "\u2726 NEW")), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, PRIORITY_STATES.map(s => /*#__PURE__*/React.createElement("button", {
    key: s,
    className: "pick",
    style: norm(value) === s ? PICK_ON : null,
    onClick: () => onChange(s)
  }, s)), selectedExtra && /*#__PURE__*/React.createElement("button", {
    className: "pick",
    style: PICK_ON,
    onClick: () => onChange('')
  }, value, " \u2715"), custom && /*#__PURE__*/React.createElement("button", {
    className: "pick",
    style: greenOn,
    onClick: () => onChange('')
  }, value, " \u2715"), /*#__PURE__*/React.createElement("button", {
    className: "pick more",
    onClick: () => setOpen(true)
  }, "\u22EF More states (", moreCount, ")"), adding ? /*#__PURE__*/React.createElement("input", {
    autoFocus: true,
    className: "pick-input",
    value: draft,
    placeholder: "New state\u2026",
    onChange: e => setDraft(e.target.value),
    onKeyDown: e => {
      if (e.key === 'Enter') commit();
      if (e.key === 'Escape') {
        setAdding(false);
        setDraft('');
      }
    },
    onBlur: commit
  }) : /*#__PURE__*/React.createElement("button", {
    className: "pick add",
    onClick: () => setAdding(true)
  }, "+ New")), open && /*#__PURE__*/React.createElement("div", {
    className: "modal-overlay",
    onClick: () => setOpen(false)
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal",
    onClick: e => e.stopPropagation()
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal-hd"
  }, /*#__PURE__*/React.createElement("h3", null, "\uD83D\uDCCD All States & Union Territories"), /*#__PURE__*/React.createElement("button", {
    className: "modal-x",
    onClick: () => setOpen(false)
  }, "\xD7")), /*#__PURE__*/React.createElement("input", {
    className: "modal-search",
    autoFocus: true,
    placeholder: "Search state\u2026",
    value: q,
    onChange: e => setQ(e.target.value)
  }), /*#__PURE__*/React.createElement("div", {
    className: "modal-body"
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal-sec-l"
  }, "States (", fStates.length, ")"), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, fStates.map(s => /*#__PURE__*/React.createElement("button", {
    key: s,
    className: "pick",
    style: norm(value) === s ? PICK_ON : null,
    onClick: () => pick(s)
  }, s))), /*#__PURE__*/React.createElement("div", {
    className: "modal-sec-l"
  }, "Union Territories (", fUTs.length, ")"), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, fUTs.map(s => /*#__PURE__*/React.createElement("button", {
    key: s,
    className: "pick",
    style: norm(value) === s ? PICK_ON : null,
    onClick: () => pick(s)
  }, s))), fStates.length === 0 && fUTs.length === 0 && /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      padding: '16px 2px'
    }
  }, "No state matches \u201C", q, "\u201D.")))));
}

/* Channel picker: 4 key channels inline + a "More" popup with the full master. */
function ChannelPicker({
  value,
  onChange
}) {
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState('');
  const [q, setQ] = useState('');
  const custom = norm(value) && !ALL_CHANNELS.map(norm).includes(norm(value));
  const selectedExtra = norm(value) && !PRIORITY_CHANNELS.includes(norm(value)) && !custom;
  const more = ALL_CHANNELS.filter(c => !PRIORITY_CHANNELS.includes(c));
  const commit = () => {
    const v = norm(draft);
    if (v) onChange(v);
    setDraft('');
    setAdding(false);
  };
  const pick = c => {
    onChange(c);
    setOpen(false);
    setQ('');
  };
  const onStyle = ac => ({
    borderColor: ac,
    background: hexToRgba(ac, .12),
    color: ac,
    boxShadow: '0 0 0 3px ' + hexToRgba(ac, .16)
  });
  const dot = c => /*#__PURE__*/React.createElement("span", {
    style: {
      width: 9,
      height: 9,
      borderRadius: 3,
      background: channelMeta(c).color,
      display: 'inline-block'
    }
  });
  useEffect(() => {
    if (!open) return;
    const h = e => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [open]);
  const fMore = more.filter(c => c.includes(norm(q)));
  return /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "Channel ", custom && /*#__PURE__*/React.createElement("span", {
    className: "new-badge"
  }, "\u2726 NEW")), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, PRIORITY_CHANNELS.map(c => /*#__PURE__*/React.createElement("button", {
    key: c,
    className: "pick",
    style: norm(value) === c ? onStyle(channelMeta(c).color) : null,
    onClick: () => onChange(c)
  }, dot(c), c)), selectedExtra && /*#__PURE__*/React.createElement("button", {
    className: "pick",
    style: onStyle(channelMeta(value).color),
    onClick: () => onChange('')
  }, dot(value), value, " \u2715"), custom && /*#__PURE__*/React.createElement("button", {
    className: "pick",
    style: onStyle(channelMeta(value).color),
    onClick: () => onChange('')
  }, dot(value), value, " \u2715"), /*#__PURE__*/React.createElement("button", {
    className: "pick more",
    onClick: () => setOpen(true)
  }, "\u22EF More channels (", more.length, ")"), adding ? /*#__PURE__*/React.createElement("input", {
    autoFocus: true,
    className: "pick-input",
    value: draft,
    placeholder: "New channel\u2026",
    onChange: e => setDraft(e.target.value),
    onKeyDown: e => {
      if (e.key === 'Enter') commit();
      if (e.key === 'Escape') {
        setAdding(false);
        setDraft('');
      }
    },
    onBlur: commit
  }) : /*#__PURE__*/React.createElement("button", {
    className: "pick add",
    onClick: () => setAdding(true)
  }, "+ New")), open && /*#__PURE__*/React.createElement("div", {
    className: "modal-overlay",
    onClick: () => setOpen(false)
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal",
    onClick: e => e.stopPropagation()
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal-hd"
  }, /*#__PURE__*/React.createElement("h3", null, "\uD83C\uDFF7\uFE0F All Channels"), /*#__PURE__*/React.createElement("button", {
    className: "modal-x",
    onClick: () => setOpen(false)
  }, "\xD7")), /*#__PURE__*/React.createElement("input", {
    className: "modal-search",
    autoFocus: true,
    placeholder: "Search channel\u2026",
    value: q,
    onChange: e => setQ(e.target.value)
  }), /*#__PURE__*/React.createElement("div", {
    className: "modal-body"
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal-sec-l"
  }, "Other channels (", fMore.length, ")"), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, fMore.map(c => /*#__PURE__*/React.createElement("button", {
    key: c,
    className: "pick",
    style: norm(value) === c ? onStyle(channelMeta(c).color) : null,
    onClick: () => pick(c)
  }, dot(c), c))), fMore.length === 0 && /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      padding: '16px 2px'
    }
  }, "No channel matches \u201C", q, "\u201D.")))));
}

/* City / District picker — shows all districts of the chosen state in a searchable
   popup (+ Add custom). Falls back to a free-text box for states without a list. */
function CityPicker({
  state,
  value,
  onChange
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState('');
  const list = DISTRICTS[norm(state)] || [];
  useEffect(() => {
    if (!open) return;
    const h = e => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [open]);
  if (!norm(state)) return /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      fontSize: 13,
      padding: '6px 2px'
    }
  }, "Pick a state first.");
  if (!list.length) return /*#__PURE__*/React.createElement("input", {
    className: "combo",
    style: {
      maxWidth: 300
    },
    placeholder: "Type city / district\u2026",
    value: value,
    onChange: e => onChange(e.target.value)
  });
  const custom = norm(value) && !list.map(norm).includes(norm(value));
  const greenOn = {
    borderColor: '#16a34a',
    background: hexToRgba('#16a34a', .12),
    color: '#16a34a',
    boxShadow: '0 0 0 3px ' + hexToRgba('#16a34a', .16)
  };
  const pick = c => {
    onChange(c);
    setOpen(false);
    setQ('');
  };
  const commit = () => {
    const v = norm(draft);
    if (v) onChange(v);
    setDraft('');
    setAdding(false);
    setOpen(false);
  };
  const f = list.filter(c => c.includes(norm(q)));
  return /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, norm(value) && /*#__PURE__*/React.createElement("button", {
    className: "pick",
    style: custom ? greenOn : PICK_ON,
    onClick: () => onChange('')
  }, value, " \u2715"), /*#__PURE__*/React.createElement("button", {
    className: "pick more",
    onClick: () => setOpen(true)
  }, "\uD83D\uDCCD ", norm(value) ? 'Change' : 'Choose', " city / district (", list.length, ")"), open && /*#__PURE__*/React.createElement("div", {
    className: "modal-overlay",
    onClick: () => setOpen(false)
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal",
    onClick: e => e.stopPropagation()
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal-hd"
  }, /*#__PURE__*/React.createElement("h3", null, "\uD83D\uDCCD ", norm(state), " \u2014 City / District"), /*#__PURE__*/React.createElement("button", {
    className: "modal-x",
    onClick: () => setOpen(false)
  }, "\xD7")), /*#__PURE__*/React.createElement("input", {
    className: "modal-search",
    autoFocus: true,
    placeholder: "Search district\u2026",
    value: q,
    onChange: e => setQ(e.target.value)
  }), /*#__PURE__*/React.createElement("div", {
    className: "modal-body"
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal-sec-l"
  }, "Districts (", f.length, ")"), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, f.map(c => /*#__PURE__*/React.createElement("button", {
    key: c,
    className: "pick",
    style: norm(value) === c ? PICK_ON : null,
    onClick: () => pick(c)
  }, c)), adding ? /*#__PURE__*/React.createElement("input", {
    autoFocus: true,
    className: "pick-input",
    value: draft,
    placeholder: "Custom city\u2026",
    onChange: e => setDraft(e.target.value),
    onKeyDown: e => {
      if (e.key === 'Enter') commit();
      if (e.key === 'Escape') {
        setAdding(false);
        setDraft('');
      }
    },
    onBlur: commit
  }) : /*#__PURE__*/React.createElement("button", {
    className: "pick add",
    onClick: () => setAdding(true)
  }, "+ Add custom")), f.length === 0 && !adding && /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      padding: '12px 2px'
    }
  }, "No district matches \u201C", q, "\u201D \u2014 use ", /*#__PURE__*/React.createElement("b", null, "+ Add custom"), ".")))));
}

/* ─── STYLE 1 — Cascade Filter Builder ────────────────────────────────────── */
function CascadeBuilder({
  rows,
  targets,
  setTarget
}) {
  const [f, setF] = useState({
    channel: '',
    state: '',
    person: ''
  });
  const optsFor = dim => {
    const r = rows.filter(x => (dim === 'channel' || !f.channel || x.channel === f.channel) && (dim === 'state' || !f.state || x.state === f.state) && (dim === 'person' || !f.person || x.person === f.person));
    return distinct(r.map(x => x[dim])).sort();
  };
  const out = rows.filter(x => (!f.channel || x.channel === f.channel) && (!f.state || x.state === f.state) && (!f.person || x.person === f.person));
  const Sel = ({
    dim,
    label
  }) => /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, label), /*#__PURE__*/React.createElement("select", {
    className: "sel",
    value: f[dim],
    onChange: e => setF({
      ...f,
      [dim]: e.target.value
    })
  }, /*#__PURE__*/React.createElement("option", {
    value: ""
  }, "\u2014 Any ", label, " \u2014"), optsFor(dim).map(o => /*#__PURE__*/React.createElement("option", {
    key: o,
    value: o
  }, o))));
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "panel-hint"
  }, "\uD83E\uDDED Pick ", /*#__PURE__*/React.createElement("b", null, "any"), " criteria in ", /*#__PURE__*/React.createElement("b", null, "any"), " order \u2014 choose a channel and it narrows the states & people; choose a person and it shows their territory. Then type a target."), /*#__PURE__*/React.createElement("div", {
    className: "cascade-grid"
  }, /*#__PURE__*/React.createElement(Sel, {
    dim: "channel",
    label: "Channel"
  }), /*#__PURE__*/React.createElement(Sel, {
    dim: "state",
    label: "State"
  }), /*#__PURE__*/React.createElement(Sel, {
    dim: "person",
    label: "ASM"
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      margin: '18px 2px 4px'
    }
  }, /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontSize: 13,
      fontWeight: 600
    }
  }, "Showing ", out.length, " of ", rows.length, " territories"), (f.channel || f.state || f.person) && /*#__PURE__*/React.createElement("button", {
    className: "btn",
    onClick: () => setF({
      channel: '',
      state: '',
      person: ''
    })
  }, "Clear filters")), out.map(r => {
    const v = targets[keyOf(r.channel, r.state)];
    return /*#__PURE__*/React.createElement("div", {
      className: "row-card",
      key: keyOf(r.channel, r.state)
    }, /*#__PURE__*/React.createElement(Avatar, {
      name: r.person
    }), /*#__PURE__*/React.createElement("div", {
      style: {
        flex: 1,
        minWidth: 0
      }
    }, /*#__PURE__*/React.createElement("div", {
      style: {
        fontWeight: 700
      }
    }, r.person), /*#__PURE__*/React.createElement("div", {
      className: "muted",
      style: {
        fontSize: 12.5,
        marginTop: 2
      }
    }, r.state)), /*#__PURE__*/React.createElement(ChannelChip, {
      ch: r.channel
    }), /*#__PURE__*/React.createElement(TargetInput, {
      value: v,
      onChange: val => setTarget(r.channel, r.state, val)
    }), v > 0 && /*#__PURE__*/React.createElement("span", {
      className: "pill"
    }, "Assigned \u2713"));
  }));
}

/* ─── STYLE 2 — Drill-Down Cards ──────────────────────────────────────────── */
function DrillCards({
  rows,
  channels,
  targets,
  setTarget,
  clearCell,
  totalsByChannel,
  chRows,
  setChField,
  admin,
  chLast,
  monthLabel,
  products,
  month,
  year,
  BOOT
}) {
  const [editProd, setEditProd] = useState(null); // {channel,state} whose product-target modal is open (nested on top)
  const [editCh, setEditCh] = useState(null); // channel whose channel-target modal is open
  const [chTab, setChTab] = useState('channel'); // tab inside the channel modal: channel | state | person
  const [savedSnap, setSavedSnap] = useState(null); // last saved targets, captured when the modal opened
  const [prodFilter, setProdFilter] = useState('all'); // product-modal filter: all | P | C
  const [prodActuals, setProdActuals] = useState({}); // {P#SUB:{litres,realise}} last-month actual sale for the open state
  const [prodActualsMo, setProdActualsMo] = useState(null);
  const openProd = (channel, state) => {
    setSavedSnap(targets[keyOf(channel, state)] || {});
    setProdActuals({});
    setProdActualsMo(null);
    setEditProd({
      channel,
      state
    });
    if (BOOT && BOOT.urls && BOOT.urls.productActuals) {
      fetch(BOOT.urls.productActuals + '?channel=' + encodeURIComponent(channel) + '&state=' + encodeURIComponent(state) + '&month=' + month + '&year=' + year, {
        headers: {
          'X-CSRFToken': BOOT.csrf
        }
      }).then(r => r.json()).then(d => {
        if (d.status === 'ok') {
          setProdActuals(d.products || {});
          setProdActualsMo({
            month: d.last_month,
            year: d.last_year
          });
        }
      }).catch(() => {});
    }
  };
  const openCh = ch => {
    setChTab('channel');
    setEditCh(ch);
  };
  useEffect(() => {
    if (!editProd) return;
    const h = e => {
      if (e.key === 'Escape') setEditProd(null);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [editProd]);
  useEffect(() => {
    if (!editCh) return;
    const h = e => {
      if (e.key === 'Escape' && !editProd) setEditCh(null);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [editCh, editProd]);
  const lastLbl = chLast ? monthLabel(chLast.month) + ' ' + chLast.year : 'last month';
  // Per-state product-target roll-up (litres) for a channel — the granular total that
  // flows UP to the channel card when no "whole" channel target is set.
  const chRollup = ch => {
    let p = 0,
      c = 0;
    rows.forEach(r => {
      if (r.channel !== ch) return;
      const tv = targets[keyOf(r.channel, r.state)];
      p += tByType(tv, 'P');
      c += tByType(tv, 'C');
    });
    return {
      p,
      c
    };
  };
  // Effective channel target = the "whole" channel target if set, else the per-state roll-up.
  const chEffective = (ch, ctr) => {
    const roll = chRollup(ch);
    const wP = parseFloat(ctr && ctr.premium_ltrs) || 0,
      wC = parseFloat(ctr && ctr.commodity_ltrs) || 0;
    return {
      p: wP > 0 ? wP : roll.p,
      c: wC > 0 ? wC : roll.c,
      wholeP: wP,
      wholeC: wC,
      rollP: roll.p,
      rollC: roll.c
    };
  };
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "ch-grid"
  }, channels.map(ch => {
    const c = channelMeta(ch);
    const n = rows.filter(r => r.channel === ch).length;
    const ctr = (chRows || []).find(r => r.channel === ch) || {};
    const pL = parseFloat(ctr.premium_ltrs) || 0,
      pR = parseFloat(ctr.premium_realise) || 0;
    const cL = parseFloat(ctr.commodity_ltrs) || 0,
      cR = parseFloat(ctr.commodity_realise) || 0;
    const eff = chEffective(ch, ctr); // whole-target if set, else per-state roll-up
    return /*#__PURE__*/React.createElement("div", {
      key: ch,
      className: "ch-tile",
      style: {
        color: c.color,
        cursor: 'pointer'
      },
      onClick: () => openCh(ch)
    }, /*#__PURE__*/React.createElement("div", {
      style: {
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center'
      }
    }, /*#__PURE__*/React.createElement("span", {
      className: "chip",
      style: {
        background: c.light,
        color: c.color
      }
    }, ch), /*#__PURE__*/React.createElement("span", {
      style: {
        fontWeight: 800,
        fontSize: 18,
        color: 'var(--tx)'
      }
    }, n)), /*#__PURE__*/React.createElement("div", {
      style: {
        fontSize: 12.5,
        color: 'var(--tx2)',
        marginTop: 8,
        fontWeight: 600
      }
    }, c.full), /*#__PURE__*/React.createElement("div", {
      className: "ch-last"
    }, "Last mo ", /*#__PURE__*/React.createElement("b", null, fmtL(ctr.last_litres || 0)), ctr.last_realise ? /*#__PURE__*/React.createElement("span", null, " @ \u20B9", ctr.last_realise) : null), /*#__PURE__*/React.createElement("div", {
      className: "ch-sum"
    }, /*#__PURE__*/React.createElement("div", {
      className: "ch-sum-row"
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        color: '#0d9488',
        fontWeight: 800
      }
    }, "Premium"), /*#__PURE__*/React.createElement("b", null, eff.p > 0 ? fmtL(eff.p) : /*#__PURE__*/React.createElement("span", {
      style: {
        color: 'var(--tx3)'
      }
    }, "\u2014 not set"), pL > 0 && pR > 0 ? /*#__PURE__*/React.createElement("span", {
      className: "ch-sum-r"
    }, " @ \u20B9", pR) : null)), /*#__PURE__*/React.createElement("div", {
      className: "ch-sum-row"
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        color: '#b45309',
        fontWeight: 800
      }
    }, "Commodity"), /*#__PURE__*/React.createElement("b", null, eff.c > 0 ? fmtL(eff.c) : /*#__PURE__*/React.createElement("span", {
      style: {
        color: 'var(--tx3)'
      }
    }, "\u2014 not set"), cL > 0 && cR > 0 ? /*#__PURE__*/React.createElement("span", {
      className: "ch-sum-r"
    }, " @ \u20B9", cR) : null)), (eff.rollP > 0 || eff.rollC > 0) && (pL > 0 || cL > 0) && /*#__PURE__*/React.createElement("div", {
      className: "ch-sum-row",
      style: {
        fontSize: 10.5,
        color: 'var(--tx3)'
      }
    }, /*#__PURE__*/React.createElement("span", null, "per-state"), /*#__PURE__*/React.createElement("span", null, fmtL(eff.rollP + eff.rollC)))));
  })), editProd && (() => {
    const k = keyOf(editProd.channel, editProd.state);
    const v = targets[k];
    const snap = savedSnap || {};
    const shown = products.filter(p => prodFilter === 'all' ? true : p.type === prodFilter);
    // litres-weighted target realise (₹/L) across products of a type ('all' = both)
    const rWtd = (obj, type) => {
      let sl = 0,
        slr = 0;
      products.forEach(p => {
        if (type !== 'all' && p.type !== type) return;
        const id = pid(p.type, p.name);
        const l = tProdL(obj, id),
          r = tProdR(obj, id);
        if (l > 0) {
          sl += l;
          slr += l * r;
        }
      });
      return sl > 0 ? Math.round(slr / sl) : 0;
    };
    // last-month actual SALE total (₹-weighted realise) from prodActuals
    let soldTot = 0,
      soldRev = 0;
    Object.values(prodActuals).forEach(a => {
      soldTot += a.litres || 0;
      soldRev += (a.litres || 0) * (a.realise || 0);
    });
    const soldRlz = soldTot > 0 ? Math.round(soldRev / soldTot) : 0;
    const clearProduct = id => {
      setTarget(editProd.channel, editProd.state, id, 'l', '');
      setTarget(editProd.channel, editProd.state, id, 'r', '');
    };
    const ProdCard = p => {
      const id = pid(p.type, p.name),
        color = hashColor(p.name + p.type);
      const lastL = tProdL(snap, id),
        lastR = tProdR(snap, id),
        hadLast = lastL > 0 || lastR > 0;
      const curL = tProdL(v, id),
        curR = tProdR(v, id),
        hasNow = curL > 0 || curR > 0;
      return /*#__PURE__*/React.createElement("div", {
        className: "prod-card",
        key: id,
        style: {
          background: color,
          backgroundImage: 'linear-gradient(135deg,rgba(255,255,255,.16),rgba(0,0,0,.22))'
        }
      }, /*#__PURE__*/React.createElement("div", {
        className: "prod-card-top"
      }, /*#__PURE__*/React.createElement("span", {
        className: "prod-badge"
      }, p.type === 'P' ? 'PREMIUM' : 'COMMODITY'), hasNow && /*#__PURE__*/React.createElement("button", {
        className: "prod-clear",
        title: "Clear this product",
        onClick: () => clearProduct(id)
      }, "Clear")), /*#__PURE__*/React.createElement("div", {
        className: "prod-name"
      }, p.name), /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          gap: 8,
          marginTop: 2
        }
      }, /*#__PURE__*/React.createElement("div", {
        style: {
          flex: 1,
          minWidth: 0
        }
      }, /*#__PURE__*/React.createElement("div", {
        className: "prod-ilbl"
      }, "TGT LITRES"), /*#__PURE__*/React.createElement("div", {
        className: "prod-input-wrap"
      }, /*#__PURE__*/React.createElement("input", {
        className: "prod-input",
        type: "number",
        min: "0",
        placeholder: "0",
        value: curL || '',
        onChange: e => setTarget(editProd.channel, editProd.state, id, 'l', e.target.value)
      }))), /*#__PURE__*/React.createElement("div", {
        style: {
          flex: 1,
          minWidth: 0
        }
      }, /*#__PURE__*/React.createElement("div", {
        className: "prod-ilbl"
      }, "TGT REALISE (\u20B9)"), /*#__PURE__*/React.createElement("div", {
        className: "prod-input-wrap"
      }, /*#__PURE__*/React.createElement("input", {
        className: "prod-input",
        type: "number",
        min: "0",
        placeholder: "0",
        value: curR || '',
        onChange: e => setTarget(editProd.channel, editProd.state, id, 'r', e.target.value)
      })))), (() => {
        const act = prodActuals[id];
        if (act && (act.litres !== 0 || act.realise !== 0)) {
          const neg = act.litres < 0;
          return /*#__PURE__*/React.createElement("div", {
            className: "prod-last",
            style: neg ? {
              background: 'rgba(220,38,38,.5)'
            } : undefined
          }, "\uD83D\uDED2 Last mo sold: ", /*#__PURE__*/React.createElement("b", {
            style: neg ? {
              color: '#fee2e2'
            } : undefined
          }, fmtL(act.litres)), act.realise > 0 ? ' · ₹' + act.realise + '/L' : '');
        }
        return /*#__PURE__*/React.createElement("div", {
          className: "prod-last"
        }, prodActualsMo ? 'No sale last month' : '…');
      })());
    };
    const group = (type, label, color) => {
      const list = products.filter(p => p.type === type);
      if (!list.length) return null;
      return /*#__PURE__*/React.createElement("div", {
        className: "prow-group"
      }, /*#__PURE__*/React.createElement("div", {
        className: "prow-grouphd",
        style: {
          color
        }
      }, label, " \u2014 ", /*#__PURE__*/React.createElement("b", null, fmtL(tByType(v, type))), rWtd(v, type) > 0 ? /*#__PURE__*/React.createElement("span", {
        style: {
          fontWeight: 700
        }
      }, " @ \u20B9", rWtd(v, type), "/L") : null, tByType(snap, type) > 0 && /*#__PURE__*/React.createElement("span", {
        style: {
          opacity: .55,
          fontWeight: 700
        }
      }, " \xB7 last ", fmtL(tByType(snap, type)), rWtd(snap, type) > 0 ? ' @ ₹' + rWtd(snap, type) : '')), /*#__PURE__*/React.createElement("div", {
        className: "prod-grid"
      }, list.map(ProdCard)));
    };
    return /*#__PURE__*/React.createElement("div", {
      className: "modal-overlay",
      style: {
        zIndex: 1100
      },
      onClick: () => setEditProd(null)
    }, /*#__PURE__*/React.createElement("div", {
      className: "modal",
      style: {
        maxWidth: 'min(1280px,96vw)',
        width: '100%',
        maxHeight: '90vh'
      },
      onClick: e => e.stopPropagation()
    }, /*#__PURE__*/React.createElement("div", {
      className: "modal-hd"
    }, /*#__PURE__*/React.createElement("h3", {
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 8
      }
    }, "\uD83C\uDFAF Targets \u2014 ", /*#__PURE__*/React.createElement(ChannelChip, {
      ch: editProd.channel
    }), " ", editProd.state), /*#__PURE__*/React.createElement("div", {
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 10
      }
    }, /*#__PURE__*/React.createElement("div", {
      className: "seg"
    }, /*#__PURE__*/React.createElement("button", {
      className: 'seg-b' + (prodFilter === 'all' ? ' on' : ''),
      onClick: () => setProdFilter('all')
    }, "All"), /*#__PURE__*/React.createElement("button", {
      className: 'seg-b' + (prodFilter === 'P' ? ' on' : ''),
      onClick: () => setProdFilter('P')
    }, "Premium"), /*#__PURE__*/React.createElement("button", {
      className: 'seg-b' + (prodFilter === 'C' ? ' on' : ''),
      onClick: () => setProdFilter('C')
    }, "Commodity")), /*#__PURE__*/React.createElement("button", {
      className: "modal-x",
      onClick: () => setEditProd(null)
    }, "\xD7"))), /*#__PURE__*/React.createElement("div", {
      className: "modal-summary"
    }, /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "Premium"), /*#__PURE__*/React.createElement("b", {
      style: {
        color: '#0d9488'
      }
    }, fmtL(tByType(v, 'P')), rWtd(v, 'P') > 0 ? /*#__PURE__*/React.createElement("span", {
      style: {
        color: 'var(--tx3)',
        fontWeight: 700
      }
    }, " @ \u20B9", rWtd(v, 'P')) : null)), /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "Commodity"), /*#__PURE__*/React.createElement("b", {
      style: {
        color: '#d97706'
      }
    }, fmtL(tByType(v, 'C')), rWtd(v, 'C') > 0 ? /*#__PURE__*/React.createElement("span", {
      style: {
        color: 'var(--tx3)',
        fontWeight: 700
      }
    }, " @ \u20B9", rWtd(v, 'C')) : null)), /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "New total"), /*#__PURE__*/React.createElement("b", null, fmtL(tSum(v)), rWtd(v, 'all') > 0 ? /*#__PURE__*/React.createElement("span", {
      style: {
        color: 'var(--tx3)',
        fontWeight: 700
      }
    }, " @ \u20B9", rWtd(v, 'all')) : null)), /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "\uD83D\uDED2 Last mo sold"), /*#__PURE__*/React.createElement("b", {
      style: {
        color: 'var(--ac)'
      }
    }, soldTot > 0 ? fmtL(soldTot) : prodActualsMo ? '0 L' : '…', soldRlz > 0 ? /*#__PURE__*/React.createElement("span", {
      style: {
        color: 'var(--tx3)',
        fontWeight: 700
      }
    }, " @ \u20B9", soldRlz) : null))), /*#__PURE__*/React.createElement("div", {
      className: "modal-body"
    }, (prodFilter === 'all' || prodFilter === 'P') && group('P', 'Premium', '#0d9488'), (prodFilter === 'all' || prodFilter === 'C') && group('C', 'Commodity', '#d97706'), shown.length === 0 && /*#__PURE__*/React.createElement("div", {
      className: "muted",
      style: {
        padding: '10px 2px'
      }
    }, "No products in this filter.")), /*#__PURE__*/React.createElement("div", {
      className: "modal-foot"
    }, /*#__PURE__*/React.createElement("button", {
      className: "btn btn-clear",
      disabled: tSum(v) === 0,
      onClick: () => {
        if (confirm('Clear ALL targets for ' + editProd.state + '?')) clearCell(editProd.channel, editProd.state);
      }
    }, "\uD83D\uDDD1 Clear all"), /*#__PURE__*/React.createElement("div", {
      style: {
        flex: 1
      }
    }), /*#__PURE__*/React.createElement("b", {
      style: {
        fontSize: 14,
        marginRight: 12
      }
    }, "Total: ", fmtL(tSum(v))), /*#__PURE__*/React.createElement("button", {
      className: "btn btn-green",
      onClick: () => setEditProd(null)
    }, "Done"))));
  })(), editCh && (() => {
    const ctr = (chRows || []).find(r => r.channel === editCh) || {};
    const cm = channelMeta(editCh);
    const eff = chEffective(editCh, ctr); // effective = whole-target if set, else per-state roll-up
    const totL = eff.p + eff.c; // effective channel total
    const wholeL = eff.wholeP + eff.wholeC; // the directly-entered "whole" total
    const hasAny = wholeL > 0 || (parseFloat(ctr.premium_realise) || 0) > 0 || (parseFloat(ctr.commodity_realise) || 0) > 0;
    const chStates = rows.filter(r => r.channel === editCh);
    const asmCount = distinct(chStates.map(r => r.person)).filter(Boolean).length;
    const perStateTot = chStates.reduce((s, r) => s + tSum(targets[keyOf(r.channel, r.state)]), 0);
    const byPerson = {};
    chStates.forEach(r => {
      const p = r.person || '— Unassigned';
      (byPerson[p] = byPerson[p] || []).push(r);
    });
    const persons = Object.keys(byPerson).sort((a, b) => a.localeCompare(b));
    const SegCard = seg => {
      const isP = seg === 'premium';
      const lk = seg + '_ltrs',
        rk = seg + '_realise';
      const lastL = ctr['last_' + seg + '_ltrs'] || 0,
        lastR = ctr['last_' + seg + '_realise'] || 0;
      const curL = ctr[lk] || '',
        curR = ctr[rk] || '';
      const bg = isP ? '#0d9488' : '#d97706';
      return /*#__PURE__*/React.createElement("div", {
        className: "prod-card",
        key: seg,
        style: {
          background: bg,
          backgroundImage: 'linear-gradient(135deg,rgba(255,255,255,.16),rgba(0,0,0,.22))'
        }
      }, /*#__PURE__*/React.createElement("div", {
        className: "prod-card-top"
      }, /*#__PURE__*/React.createElement("span", {
        className: "prod-badge"
      }, isP ? 'PREMIUM' : 'COMMODITY'), (curL !== '' || curR !== '') && /*#__PURE__*/React.createElement("button", {
        className: "prod-clear",
        onClick: () => {
          setChField(editCh, lk, '');
          setChField(editCh, rk, '');
        }
      }, "Clear")), /*#__PURE__*/React.createElement("div", {
        className: "prod-name"
      }, cm.full), (() => {
        const e = isP ? eff.p : eff.c,
          w = isP ? eff.wholeP : eff.wholeC,
          rl = isP ? eff.rollP : eff.rollC;
        return /*#__PURE__*/React.createElement("div", {
          className: "seg-eff"
        }, /*#__PURE__*/React.createElement("b", null, fmtL(e)), /*#__PURE__*/React.createElement("span", {
          className: "seg-eff-src"
        }, w > 0 ? 'whole target' : rl > 0 ? '↑ from per-state' : 'not set yet'));
      })(), /*#__PURE__*/React.createElement("div", {
        className: "prod-sub"
      }, "Set / override whole-channel total:"), /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          gap: 10
        }
      }, /*#__PURE__*/React.createElement("div", {
        style: {
          flex: 1,
          minWidth: 0
        }
      }, /*#__PURE__*/React.createElement("div", {
        className: "prod-ilbl"
      }, "TGT LITRES"), /*#__PURE__*/React.createElement("div", {
        className: "prod-input-wrap"
      }, /*#__PURE__*/React.createElement("input", {
        className: "prod-input",
        type: "number",
        min: "0",
        disabled: !admin,
        placeholder: "0",
        value: curL,
        onChange: e => setChField(editCh, lk, e.target.value)
      }))), /*#__PURE__*/React.createElement("div", {
        style: {
          flex: 1,
          minWidth: 0
        }
      }, /*#__PURE__*/React.createElement("div", {
        className: "prod-ilbl"
      }, "TGT REALISE (\u20B9/L)"), /*#__PURE__*/React.createElement("div", {
        className: "prod-input-wrap"
      }, /*#__PURE__*/React.createElement("input", {
        className: "prod-input",
        type: "number",
        min: "0",
        step: "0.01",
        disabled: !admin,
        placeholder: "0",
        value: curR,
        onChange: e => setChField(editCh, rk, e.target.value)
      })))), /*#__PURE__*/React.createElement("div", {
        className: "prod-last"
      }, lastL > 0 || lastR > 0 ? /*#__PURE__*/React.createElement(React.Fragment, null, "\u21A9 Last month sale: ", /*#__PURE__*/React.createElement("b", null, fmtL(lastL)), lastR > 0 ? ' · ₹' + lastR + '/L' : '') : 'No sale last month'), admin && /*#__PURE__*/React.createElement("button", {
        className: "seg-uselast",
        disabled: !(lastL > 0 || lastR > 0),
        onClick: () => {
          setChField(editCh, lk, Math.round(lastL));
          setChField(editCh, rk, lastR);
        }
      }, "\u21BA use last month"));
    };
    const StateCard = r => {
      const tv = targets[keyOf(r.channel, r.state)];
      const has = tSum(tv) > 0;
      return /*#__PURE__*/React.createElement("div", {
        className: "state-card",
        key: r.channel + '|' + r.state
      }, /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          marginBottom: 11
        }
      }, /*#__PURE__*/React.createElement(Avatar, {
        name: r.person,
        size: 30
      }), /*#__PURE__*/React.createElement("div", {
        style: {
          minWidth: 0,
          flex: 1
        }
      }, /*#__PURE__*/React.createElement("div", {
        style: {
          fontWeight: 700,
          fontSize: 14
        }
      }, r.state), /*#__PURE__*/React.createElement("div", {
        className: "muted",
        style: {
          fontSize: 12
        }
      }, r.person || 'Unassigned')), admin && /*#__PURE__*/React.createElement("button", {
        className: "tcard-clear",
        disabled: !has,
        title: has ? 'Clear all targets for this state' : 'No targets to clear',
        onClick: () => {
          if (has && confirm('Clear all targets for ' + r.state + '?')) clearCell(r.channel, r.state);
        }
      }, "Clear")), /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          flexDirection: 'column',
          gap: 6
        }
      }, /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: 12.5
        }
      }, /*#__PURE__*/React.createElement("span", {
        style: {
          fontWeight: 700,
          color: '#0d9488'
        }
      }, "Premium"), /*#__PURE__*/React.createElement("b", null, fmtL(tByType(tv, 'P')))), /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: 12.5
        }
      }, /*#__PURE__*/React.createElement("span", {
        style: {
          fontWeight: 700,
          color: '#d97706'
        }
      }, "Commodity"), /*#__PURE__*/React.createElement("b", null, fmtL(tByType(tv, 'C')))), /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          borderTop: '1px dashed var(--border)',
          paddingTop: 6
        }
      }, /*#__PURE__*/React.createElement("span", {
        className: "muted",
        style: {
          fontSize: 10,
          fontWeight: 800,
          textTransform: 'uppercase',
          letterSpacing: '.4px'
        }
      }, "Total"), /*#__PURE__*/React.createElement("b", {
        style: {
          fontSize: 13.5
        }
      }, fmtL(tSum(tv)))), /*#__PURE__*/React.createElement("button", {
        className: "btn btn-green",
        style: {
          padding: '8px 12px',
          fontSize: 12.5,
          marginTop: 4,
          width: '100%'
        },
        onClick: () => openProd(r.channel, r.state)
      }, "\uD83C\uDFAF Set product targets")));
    };
    return /*#__PURE__*/React.createElement("div", {
      className: "modal-overlay",
      onClick: () => setEditCh(null)
    }, /*#__PURE__*/React.createElement("div", {
      className: "modal",
      style: {
        maxWidth: chTab === 'channel' ? 'min(1040px,96vw)' : 'min(1360px,97vw)',
        width: '100%',
        minHeight: 'min(560px,82vh)',
        maxHeight: '92vh'
      },
      onClick: e => e.stopPropagation()
    }, /*#__PURE__*/React.createElement("div", {
      className: "modal-hd"
    }, /*#__PURE__*/React.createElement("h3", {
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 8
      }
    }, "\uD83C\uDFAF Targets \u2014 ", /*#__PURE__*/React.createElement(ChannelChip, {
      ch: editCh
    }), " ", cm.full), /*#__PURE__*/React.createElement("div", {
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 10
      }
    }, /*#__PURE__*/React.createElement("div", {
      className: "seg"
    }, /*#__PURE__*/React.createElement("button", {
      className: 'seg-b' + (chTab === 'channel' ? ' on' : ''),
      onClick: () => setChTab('channel')
    }, "Channel"), /*#__PURE__*/React.createElement("button", {
      className: 'seg-b' + (chTab === 'state' ? ' on' : ''),
      onClick: () => setChTab('state')
    }, "\uD83D\uDCCD State (", chStates.length, ")"), /*#__PURE__*/React.createElement("button", {
      className: 'seg-b' + (chTab === 'person' ? ' on' : ''),
      onClick: () => setChTab('person')
    }, "\uD83D\uDC64 Person (", asmCount, ")")), /*#__PURE__*/React.createElement("button", {
      className: "modal-x",
      onClick: () => setEditCh(null)
    }, "\xD7"))), chTab === 'channel' && /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("div", {
      className: "modal-summary"
    }, /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "Last month total"), /*#__PURE__*/React.createElement("b", null, fmtL(ctr.last_litres || 0), ctr.last_realise ? ' · ₹' + ctr.last_realise : '')), /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "Premium ", eff.wholeP > 0 ? '(whole)' : eff.rollP > 0 ? '(per-state)' : ''), /*#__PURE__*/React.createElement("b", {
      style: {
        color: '#0d9488'
      }
    }, fmtL(eff.p))), /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "Commodity ", eff.wholeC > 0 ? '(whole)' : eff.rollC > 0 ? '(per-state)' : ''), /*#__PURE__*/React.createElement("b", {
      style: {
        color: '#d97706'
      }
    }, fmtL(eff.c))), /*#__PURE__*/React.createElement("div", {
      className: "ms-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "ms-l"
    }, "Channel total"), /*#__PURE__*/React.createElement("b", null, fmtL(totL)))), /*#__PURE__*/React.createElement("div", {
      className: "modal-body"
    }, /*#__PURE__*/React.createElement("div", {
      className: "prod-grid",
      style: {
        gridTemplateColumns: 'repeat(2,minmax(0,1fr))'
      }
    }, SegCard('premium'), SegCard('commodity')))), chTab === 'state' && /*#__PURE__*/React.createElement("div", {
      className: "modal-body"
    }, chStates.length ? /*#__PURE__*/React.createElement("div", {
      className: "state-grid"
    }, chStates.map(StateCard)) : /*#__PURE__*/React.createElement("div", {
      className: "muted",
      style: {
        padding: '10px 2px'
      }
    }, "No assigned states in this channel yet.")), chTab === 'person' && /*#__PURE__*/React.createElement("div", {
      className: "modal-body"
    }, persons.length ? persons.map(p => {
      const grp = byPerson[p];
      const pTot = grp.reduce((s, r) => s + tSum(targets[keyOf(r.channel, r.state)]), 0);
      return /*#__PURE__*/React.createElement("div", {
        key: p,
        className: "person-group"
      }, /*#__PURE__*/React.createElement("div", {
        className: "person-hd"
      }, /*#__PURE__*/React.createElement(Avatar, {
        name: p === '— Unassigned' ? '' : p,
        size: 28
      }), /*#__PURE__*/React.createElement("div", {
        className: "person-hd-name"
      }, p), /*#__PURE__*/React.createElement("span", {
        className: "person-hd-meta"
      }, grp.length, " state", grp.length > 1 ? 's' : '', " \xB7 ", fmtL(pTot))), /*#__PURE__*/React.createElement("div", {
        className: "state-grid"
      }, grp.map(StateCard)));
    }) : /*#__PURE__*/React.createElement("div", {
      className: "muted",
      style: {
        padding: '10px 2px'
      }
    }, "No ASMs assigned in this channel yet.")), /*#__PURE__*/React.createElement("div", {
      className: "modal-foot"
    }, chTab === 'channel' ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("button", {
      className: "btn btn-clear",
      disabled: !hasAny || !admin,
      onClick: () => {
        if (confirm('Clear channel target for ' + editCh + '?')) {
          setChField(editCh, 'premium_ltrs', '');
          setChField(editCh, 'premium_realise', '');
          setChField(editCh, 'commodity_ltrs', '');
          setChField(editCh, 'commodity_realise', '');
        }
      }
    }, "\uD83D\uDDD1 Clear"), /*#__PURE__*/React.createElement("div", {
      style: {
        flex: 1
      }
    }), /*#__PURE__*/React.createElement("b", {
      style: {
        fontSize: 14,
        marginRight: 12
      }
    }, "Channel total: ", fmtL(totL))) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
      className: "muted",
      style: {
        fontSize: 12
      }
    }, "Click a state's ", /*#__PURE__*/React.createElement("b", null, "Set product targets"), " to set Premium/Commodity per product."), /*#__PURE__*/React.createElement("div", {
      style: {
        flex: 1
      }
    }), /*#__PURE__*/React.createElement("b", {
      style: {
        fontSize: 14,
        marginRight: 12
      }
    }, "Per-state total: ", fmtL(perStateTot))), /*#__PURE__*/React.createElement("button", {
      className: "btn btn-green",
      onClick: () => setEditCh(null)
    }, "Done"))));
  })());
}

/* ─── STYLE 3 — Pivot Matrix / Heatmap ────────────────────────────────────── */
function Matrix({
  rows,
  channels,
  targets,
  setTarget
}) {
  const states = distinct(rows.map(r => r.state)).sort();
  const combo = {};
  rows.forEach(r => {
    combo[keyOf(r.channel, r.state)] = r.person;
  });
  const max = Math.max(1, ...Object.values(targets).map(Number).filter(Boolean));
  const colTotal = ch => rows.filter(r => r.channel === ch).reduce((s, r) => s + (+targets[keyOf(ch, r.state)] || 0), 0);
  const rowTotal = st => channels.reduce((s, ch) => s + (+targets[keyOf(ch, st)] || 0), 0);
  const grand = states.reduce((s, st) => s + rowTotal(st), 0);
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "panel-hint"
  }, "\uD83D\uDCCA The classic management view: ", /*#__PURE__*/React.createElement("b", null, "states \xD7 channels"), ". Each filled cell is an owned territory \u2014 type a target and the cell heats up. Totals roll up live."), /*#__PURE__*/React.createElement("div", {
    style: {
      overflowX: 'auto'
    }
  }, /*#__PURE__*/React.createElement("table", {
    className: "matrix"
  }, /*#__PURE__*/React.createElement("thead", null, /*#__PURE__*/React.createElement("tr", null, /*#__PURE__*/React.createElement("th", {
    style: {
      textAlign: 'left'
    }
  }, "State"), channels.map(ch => /*#__PURE__*/React.createElement("th", {
    key: ch
  }, /*#__PURE__*/React.createElement(ChannelChip, {
    ch: ch
  }))), /*#__PURE__*/React.createElement("th", null, "Total"))), /*#__PURE__*/React.createElement("tbody", null, states.map(st => /*#__PURE__*/React.createElement("tr", {
    key: st
  }, /*#__PURE__*/React.createElement("td", {
    className: "state-h"
  }, st), channels.map(ch => {
    const person = combo[keyOf(ch, st)];
    if (!person) return /*#__PURE__*/React.createElement("td", {
      key: ch,
      className: "empty"
    }, "\xB7");
    const v = +targets[keyOf(ch, st)] || 0;
    return /*#__PURE__*/React.createElement("td", {
      key: ch,
      style: {
        background: v ? hexToRgba('#4f46e5', 0.06 + v / max * 0.32) : '#fff'
      }
    }, /*#__PURE__*/React.createElement("div", {
      style: {
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 4
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        fontSize: 10.5,
        fontWeight: 700,
        color: hashColor(person)
      }
    }, initials(person)), /*#__PURE__*/React.createElement("input", {
      className: "cell-input",
      type: "number",
      min: "0",
      step: "10000",
      placeholder: "\u2014",
      value: targets[keyOf(ch, st)] ?? '',
      onChange: e => setTarget(ch, st, e.target.value)
    })));
  }), /*#__PURE__*/React.createElement("td", {
    className: "total-col"
  }, rowTotal(st) ? fmt(rowTotal(st)) : '—')))), /*#__PURE__*/React.createElement("tfoot", null, /*#__PURE__*/React.createElement("tr", null, /*#__PURE__*/React.createElement("td", {
    style: {
      textAlign: 'left'
    }
  }, "Channel total"), channels.map(ch => /*#__PURE__*/React.createElement("td", {
    key: ch
  }, colTotal(ch) ? fmt(colTotal(ch)) : '—')), /*#__PURE__*/React.createElement("td", {
    style: {
      background: 'var(--ac-light)',
      color: 'var(--ac)'
    }
  }, fmt(grand)))))));
}

/* ─── STYLE 4 — Tree / Org Hierarchy ──────────────────────────────────────── */
function Tree({
  rows,
  channels,
  targets,
  setTarget
}) {
  const [open, setOpen] = useState(() => ({
    [channels[0]]: true
  }));
  const toggle = k => setOpen(o => ({
    ...o,
    [k]: !o[k]
  }));
  const chTotal = ch => rows.filter(r => r.channel === ch).reduce((s, r) => s + (+targets[keyOf(ch, r.state)] || 0), 0);
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "panel-hint"
  }, "\uD83C\uDF33 Roll-up ownership: ", /*#__PURE__*/React.createElement("b", null, "Channel \u2192 Salesperson \u2192 State"), ". Great for \u201Cwho owns what\u201D conversations, with subtotals at every level."), channels.map(ch => {
    const people = distinct(rows.filter(r => r.channel === ch).map(r => r.person));
    return /*#__PURE__*/React.createElement("div", {
      key: ch
    }, /*#__PURE__*/React.createElement("div", {
      className: "tree-row",
      onClick: () => toggle(ch),
      style: {
        fontWeight: 800
      }
    }, /*#__PURE__*/React.createElement("span", {
      className: "tree-chev"
    }, open[ch] ? '▼' : '▶'), /*#__PURE__*/React.createElement(ChannelChip, {
      ch: ch
    }), /*#__PURE__*/React.createElement("span", null, channelMeta(ch).full), /*#__PURE__*/React.createElement("span", {
      style: {
        marginLeft: 'auto',
        color: 'var(--ac)',
        fontWeight: 800
      }
    }, fmt(chTotal(ch)))), open[ch] && /*#__PURE__*/React.createElement("div", {
      className: "tree-kids"
    }, people.map(p => {
      const pk = ch + '|' + p;
      const prows = rows.filter(r => r.channel === ch && r.person === p);
      const ptot = prows.reduce((s, r) => s + (+targets[keyOf(ch, r.state)] || 0), 0);
      return /*#__PURE__*/React.createElement("div", {
        key: pk
      }, /*#__PURE__*/React.createElement("div", {
        className: "tree-row",
        onClick: () => toggle(pk),
        style: {
          fontWeight: 700
        }
      }, /*#__PURE__*/React.createElement("span", {
        className: "tree-chev"
      }, open[pk] ? '▼' : '▶'), /*#__PURE__*/React.createElement(Avatar, {
        name: p,
        size: 26
      }), /*#__PURE__*/React.createElement("span", null, p), /*#__PURE__*/React.createElement("span", {
        className: "muted",
        style: {
          fontSize: 12
        }
      }, "\xB7 ", prows.length, " state", prows.length > 1 ? 's' : ''), /*#__PURE__*/React.createElement("span", {
        style: {
          marginLeft: 'auto',
          fontWeight: 700
        }
      }, fmt(ptot))), open[pk] && /*#__PURE__*/React.createElement("div", {
        className: "tree-kids"
      }, prows.map(r => /*#__PURE__*/React.createElement("div", {
        className: "tree-leaf",
        key: r.state
      }, /*#__PURE__*/React.createElement("span", {
        style: {
          fontSize: 13.5
        }
      }, "\uD83D\uDCCD ", r.state), /*#__PURE__*/React.createElement(TargetInput, {
        value: targets[keyOf(ch, r.state)],
        onChange: val => setTarget(ch, r.state, val)
      })))));
    })));
  }));
}

/* ─── STYLE 5 — Assignment Board (Kanban) ─────────────────────────────────── */
function Board({
  rows,
  channels,
  targets,
  setTarget,
  totalsByChannel
}) {
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "panel-hint"
  }, "\uD83E\uDDF1 A planning board \u2014 one column per ", /*#__PURE__*/React.createElement("b", null, "channel"), ", one card per ", /*#__PURE__*/React.createElement("b", null, "salesperson"), ", a target per state, with live column totals. The \u201Cwhiteboard\u201D management can sign off on."), /*#__PURE__*/React.createElement("div", {
    className: "board"
  }, channels.map(ch => {
    const people = distinct(rows.filter(r => r.channel === ch).map(r => r.person));
    const c = channelMeta(ch);
    return /*#__PURE__*/React.createElement("div", {
      className: "board-col",
      key: ch
    }, /*#__PURE__*/React.createElement("div", {
      className: "board-col-hd"
    }, /*#__PURE__*/React.createElement("span", {
      className: "chip",
      style: {
        background: c.light,
        color: c.color
      }
    }, ch), /*#__PURE__*/React.createElement("span", {
      style: {
        fontWeight: 800,
        fontSize: 13
      }
    }, fmt(totalsByChannel[ch] || 0))), people.map(p => {
      const prows = rows.filter(r => r.channel === ch && r.person === p);
      const ptot = prows.reduce((s, r) => s + (+targets[keyOf(ch, r.state)] || 0), 0);
      return /*#__PURE__*/React.createElement("div", {
        className: "board-card",
        key: p
      }, /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: 9,
          marginBottom: 8
        }
      }, /*#__PURE__*/React.createElement(Avatar, {
        name: p,
        size: 28
      }), /*#__PURE__*/React.createElement("span", {
        style: {
          fontWeight: 700,
          fontSize: 13.5
        }
      }, p), /*#__PURE__*/React.createElement("span", {
        style: {
          marginLeft: 'auto',
          fontSize: 12,
          fontWeight: 700,
          color: 'var(--ac)'
        }
      }, fmt(ptot))), prows.map(r => /*#__PURE__*/React.createElement("div", {
        className: "board-state",
        key: r.state
      }, /*#__PURE__*/React.createElement("span", {
        style: {
          fontSize: 12.5
        },
        className: "muted"
      }, r.state), /*#__PURE__*/React.createElement(TargetInput, {
        value: targets[keyOf(ch, r.state)],
        onChange: val => setTarget(ch, r.state, val),
        width: 92
      }))));
    }));
  })));
}

/* Current mappings shown as grouped cards (by salesperson or by channel) — no table.
   In "By channel" view each card has a + button → popup to add a person+state to it. */
function MappingCards({
  rows,
  channels,
  targets,
  removeRow,
  addRow,
  sos,
  addSO,
  removeSO,
  moveTerritory,
  swapOwners,
  undo,
  histLen,
  restoreSeed,
  showAdd,
  view
}) {
  const [grp, setGrp] = useState('person');
  const [drag, setDrag] = useState(null); // territory being dragged {person,channel,state}
  const [dragOver, setDragOver] = useState(null); // channel code being hovered as drop target (card)
  const [dragOverRow, setDragOverRow] = useState(null); // row key being hovered (swap/fill)
  const [editMode, setEditMode] = useState(false); // click-to-swap edit mode (By channel)
  const [sel, setSel] = useState([]); // up to 2 selected row keys
  const toggleSel = rk => setSel(s => s.includes(rk) ? s.filter(x => x !== rk) : s.length < 2 ? [...s, rk] : [s[1], rk]);
  const closeEdit = () => {
    setEditMode(false);
    setSel([]);
  };
  const doChange = () => {
    if (sel.length !== 2) return;
    const [a, b] = sel.map(k => k.split('||'));
    swapOwners(a[0], a[1], b[0], b[1]);
    setSel([]);
  };
  const rowLabel = rk => {
    const [c, st] = rk.split('||');
    const r = rows.find(x => x.channel === c && x.state === st);
    return r ? `${r.person || 'Unassigned'} · ${st}` : st;
  };
  const [addTo, setAddTo] = useState(null); // {mode:'channel'|'asm', value} whose + popup is open
  const [ap, setAp] = useState(''); // chosen ASM (channel-mode)
  const [ac, setAc] = useState(''); // chosen channel (asm-mode)
  const [ast, setAst] = useState(''); // chosen state
  const [aNew, setANew] = useState(false);
  const [aDraft, setADraft] = useState('');
  const [showAll, setShowAll] = useState(false);
  const [showAllCh, setShowAllCh] = useState(false);
  const [exp, setExp] = useState({}); // expanded territories (By ASM)
  const [soDraft, setSoDraft] = useState({}); // {terrKey:{so,city}}
  const [soName, setSoName] = useState(''); // SO reverse-cascade — new SO name
  const [soCh, setSoCh] = useState(''); //   → channel
  const [soSt, setSoSt] = useState(''); //   → state (filtered by channel)
  const [soCity, setSoCity] = useState(''); //   → optional city
  const [soShowAllCh, setSoShowAllCh] = useState(false);
  const people = distinct(rows.map(r => r.person)).filter(Boolean).sort();
  const allStates = [...INDIA_STATES, ...INDIA_UTS].sort();
  const customAp = norm(ap) && !people.map(norm).includes(norm(ap));
  const chOrder = ch => {
    const i = channels.indexOf(ch);
    return i < 0 ? 999 : i;
  };
  const sosFor = (ch, st) => sos.filter(s => s.channel === ch && s.state === st);
  const groups = grp === 'person' ? people.map(p => ({
    key: 'p_' + p,
    head: 'person',
    title: p,
    items: rows.filter(r => r.person === p).sort((a, b) => chOrder(a.channel) - chOrder(b.channel) || a.state.localeCompare(b.state))
  })) : channels.map(ch => ({
    key: 'c_' + ch,
    head: 'channel',
    title: ch,
    // Assigned states first, then unassigned — alphabetical by state within each group.
    items: rows.filter(r => r.channel === ch).sort((a, b) => (a.person ? 0 : 1) - (b.person ? 0 : 1) || a.state.localeCompare(b.state))
  })).filter(g => g.items.length);
  const closeAdd = () => {
    setAddTo(null);
    setAp('');
    setAc('');
    setAst('');
    setANew(false);
    setADraft('');
    setShowAll(false);
    setShowAllCh(false);
  };
  const commitNew = () => {
    const v = norm(aDraft);
    if (v) setAp(v);
    setADraft('');
    setANew(false);
  };
  const doAdd = () => {
    if (!addTo) return;
    if (addTo.mode === 'fill') {
      if (!norm(ap)) return;
      addRow({
        person: ap,
        channel: addTo.value,
        state: addTo.state
      });
    } else if (addTo.mode === 'channel') {
      if (!(norm(ap) && norm(ast))) return;
      addRow({
        person: ap,
        channel: addTo.value,
        state: ast
      });
    } else {
      if (!(norm(ac) && norm(ast))) return;
      addRow({
        person: addTo.value,
        channel: ac,
        state: ast
      });
    }
    closeAdd();
  };
  useEffect(() => {
    if (!addTo) return;
    const h = e => {
      if (e.key === 'Escape') closeAdd();
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [addTo]);
  const greenOn = {
    borderColor: '#16a34a',
    background: hexToRgba('#16a34a', .12),
    color: '#16a34a',
    boxShadow: '0 0 0 3px ' + hexToRgba('#16a34a', .16)
  };
  const chStyle = c => ({
    borderColor: channelMeta(c).color,
    background: hexToRgba(channelMeta(c).color, .12),
    color: channelMeta(c).color,
    boxShadow: '0 0 0 3px ' + hexToRgba(channelMeta(c).color, .16)
  });
  const canAdd = addTo ? addTo.mode === 'fill' ? !!norm(ap) : addTo.mode === 'channel' ? norm(ap) && norm(ast) : norm(ac) && norm(ast) : false;
  const addSummary = addTo ? addTo.mode === 'fill' ? `${norm(ap)} · ${addTo.value} · ${addTo.state}` : addTo.mode === 'channel' ? `${norm(ap)} · ${addTo.value} · ${norm(ast)}` : `${addTo.value} · ${norm(ac)} · ${norm(ast)}` : '';
  const toggleExp = k => setExp(e => ({
    ...e,
    [k]: !e[k]
  }));
  const setDraft = (k, f, v) => setSoDraft(d => ({
    ...d,
    [k]: {
      ...(d[k] || {
        so: '',
        city: ''
      }),
      [f]: v
    }
  }));
  const submitSO = (ch, st) => {
    const k = keyOf(ch, st);
    const d = soDraft[k] || {};
    if (norm(d.so) && norm(d.city)) {
      addSO(ch, st, d.so, d.city);
      setSoDraft(s => ({
        ...s,
        [k]: {
          so: '',
          city: ''
        }
      }));
    }
  };
  // ── SO tab (reverse cascade): channel → states it serves → owning ASM (auto) ──
  const soStates = norm(soCh) ? distinct(rows.filter(r => r.channel === norm(soCh)).map(r => r.state)).sort() : [];
  const soAsm = norm(soCh) && norm(soSt) ? (rows.find(r => r.channel === norm(soCh) && r.state === norm(soSt)) || {}).person || '' : '';
  const soReady = norm(soName) && norm(soCh) && norm(soSt) && soAsm;
  const resetSo = () => {
    setSoName('');
    setSoCh('');
    setSoSt('');
    setSoCity('');
    setSoShowAllCh(false);
  };
  const submitNewSO = () => {
    if (!soReady) return;
    addSO(norm(soCh), norm(soSt), norm(soName), norm(soCity));
    resetSo();
  };
  const soByAsm = (() => {
    const m = {};
    sos.forEach(s => {
      const owner = (rows.find(r => r.channel === s.channel && r.state === s.state) || {}).person || '— Unassigned';
      (m[owner] = m[owner] || []).push(s);
    });
    return m;
  })();
  return /*#__PURE__*/React.createElement("div", null, view === 'asm' && /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      margin: '18px 2px 12px',
      flexWrap: 'wrap',
      gap: 10
    }
  }, /*#__PURE__*/React.createElement("b", {
    style: {
      fontSize: 14
    }
  }, "Current mappings ", /*#__PURE__*/React.createElement("span", {
    className: "muted"
  }, "(", rows.length, ")")), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      flexWrap: 'wrap'
    }
  }, /*#__PURE__*/React.createElement("button", {
    className: "btn",
    disabled: !histLen,
    onClick: undo,
    title: "Undo last change"
  }, "\u21B6 Undo", histLen ? ` (${histLen})` : ''), /*#__PURE__*/React.createElement("button", {
    className: "btn",
    onClick: restoreSeed,
    title: "Restore the original mapping"
  }, "\u27F3 Restore"), /*#__PURE__*/React.createElement("div", {
    className: "seg"
  }, /*#__PURE__*/React.createElement("button", {
    className: 'seg-b' + (grp === 'person' ? ' on' : ''),
    onClick: () => {
      setGrp('person');
      closeEdit();
    }
  }, "By ASM"), /*#__PURE__*/React.createElement("button", {
    className: 'seg-b' + (grp === 'channel' ? ' on' : ''),
    onClick: () => {
      setGrp('channel');
      closeEdit();
    }
  }, "By channel")))), view === 'asm' && grp === 'channel' && /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: 10,
      margin: '-2px 2px 12px',
      flexWrap: 'wrap'
    }
  }, /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      fontSize: 12.5,
      display: 'flex',
      alignItems: 'center',
      gap: 6,
      flexWrap: 'wrap'
    }
  }, editMode ? sel.length === 2 ? /*#__PURE__*/React.createElement(React.Fragment, null, "\u270F\uFE0F Swap ", /*#__PURE__*/React.createElement("b", {
    style: {
      color: 'var(--ac)'
    }
  }, rowLabel(sel[0])), " \u2194 ", /*#__PURE__*/React.createElement("b", {
    style: {
      color: 'var(--ac)'
    }
  }, rowLabel(sel[1])), " \u2014 click ", /*#__PURE__*/React.createElement("b", null, "Change"), ".") : /*#__PURE__*/React.createElement(React.Fragment, null, "\u270F\uFE0F ", /*#__PURE__*/React.createElement("b", {
    style: {
      color: 'var(--ac)'
    }
  }, "Edit mode"), " \u2014 ", /*#__PURE__*/React.createElement("b", null, "drag"), " to move/swap, or click ", /*#__PURE__*/React.createElement("b", null, "two rows"), " (", sel.length, "/2) then ", /*#__PURE__*/React.createElement("b", null, "Change"), ". Use ", /*#__PURE__*/React.createElement("b", null, "+ / \xD7"), " to add/remove.") : /*#__PURE__*/React.createElement(React.Fragment, null, "\uD83D\uDC41\uFE0F ", /*#__PURE__*/React.createElement("b", {
    style: {
      color: 'var(--ac)'
    }
  }, "View only"), " \u2014 click a ", /*#__PURE__*/React.createElement("b", {
    style: {
      color: '#b45309'
    }
  }, "blank state"), " to assign an ASM, or ", /*#__PURE__*/React.createElement("b", null, "\u270F\uFE0F Edit"), " to move, swap, add or remove.")), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 8,
      flexShrink: 0
    }
  }, editMode ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("button", {
    className: "btn btn-primary",
    disabled: sel.length !== 2,
    onClick: doChange
  }, "\uD83D\uDD04 Change"), /*#__PURE__*/React.createElement("button", {
    className: "btn",
    onClick: closeEdit
  }, "\u2716 Close")) : /*#__PURE__*/React.createElement("button", {
    className: "btn",
    onClick: () => setEditMode(true)
  }, "\u270F\uFE0F Edit"))), view === 'so' && /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "add-card",
    style: showAdd ? null : {
      display: 'none'
    }
  }, /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "Step 1 \xB7 SO name ", norm(soName) && /*#__PURE__*/React.createElement("span", {
    className: "new-badge"
  }, "\u2726 NEW")), /*#__PURE__*/React.createElement("input", {
    className: "combo",
    style: {
      maxWidth: 300
    },
    placeholder: "Enter new Sales Officer name\u2026",
    value: soName,
    onChange: e => setSoName(e.target.value)
  })), /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "Step 2 \xB7 Channel"), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, (soShowAllCh ? ALL_CHANNELS : PRIORITY_CHANNELS).map(c => /*#__PURE__*/React.createElement("button", {
    key: c,
    className: "pick",
    style: norm(soCh) === c ? chStyle(c) : null,
    onClick: () => {
      setSoCh(c);
      setSoSt('');
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      width: 9,
      height: 9,
      borderRadius: 3,
      background: channelMeta(c).color,
      display: 'inline-block'
    }
  }), c)), !soShowAllCh && /*#__PURE__*/React.createElement("button", {
    className: "pick more",
    onClick: () => setSoShowAllCh(true)
  }, "\u22EF Show all (", ALL_CHANNELS.length, ")"))), /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "Step 3 \xB7 State ", norm(soCh) && /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      textTransform: 'none',
      fontWeight: 600,
      letterSpacing: 0
    }
  }, "\u2014 served by ", norm(soCh))), !norm(soCh) ? /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      fontSize: 13,
      padding: '6px 2px'
    }
  }, "\u2191 Pick a channel first.") : soStates.length === 0 ? /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      fontSize: 13,
      padding: '6px 2px'
    }
  }, "No states mapped to ", norm(soCh), " yet \u2014 add a territory in the ", /*#__PURE__*/React.createElement("b", null, "ASM"), " view first.") : /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, soStates.map(s => /*#__PURE__*/React.createElement("button", {
    key: s,
    className: "pick",
    style: norm(soSt) === s ? PICK_ON : null,
    onClick: () => {
      setSoSt(s);
      setSoCity('');
    }
  }, s)))), /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "Step 4 \xB7 Reports to (ASM)"), soAsm ? /*#__PURE__*/React.createElement("span", {
    className: "pick",
    style: {
      ...greenOn,
      cursor: 'default'
    }
  }, /*#__PURE__*/React.createElement(Avatar, {
    name: soAsm,
    size: 18
  }), soAsm, /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontWeight: 800,
      fontSize: 9.5,
      marginLeft: 2
    }
  }, "AUTO")) : /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      fontSize: 13,
      padding: '6px 2px'
    }
  }, "Auto-fills once channel & state are chosen.")), /*#__PURE__*/React.createElement("div", {
    className: "pick-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "City / District ", /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      textTransform: 'none',
      fontWeight: 600,
      letterSpacing: 0
    }
  }, "(optional)")), /*#__PURE__*/React.createElement(CityPicker, {
    state: soSt,
    value: soCity,
    onChange: setSoCity
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 10,
      alignItems: 'center',
      marginTop: 8
    }
  }, /*#__PURE__*/React.createElement("button", {
    className: "btn btn-green",
    style: {
      padding: '12px 24px',
      fontSize: 14
    },
    disabled: !soReady,
    onClick: submitNewSO
  }, "+ Add SO"), /*#__PURE__*/React.createElement("button", {
    className: "btn",
    disabled: !(soName || soCh || soSt || soCity),
    onClick: resetSo
  }, "Clear")), soReady && /*#__PURE__*/React.createElement("div", {
    className: "preview",
    style: {
      marginTop: 14,
      flexWrap: 'wrap'
    }
  }, /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontSize: 12,
      fontWeight: 700
    }
  }, "PREVIEW"), /*#__PURE__*/React.createElement("span", {
    className: "mini-av",
    style: {
      background: hashColor(norm(soName))
    }
  }, initials(norm(soName))), /*#__PURE__*/React.createElement("b", null, norm(soName)), /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontSize: 10.5,
      fontWeight: 800
    }
  }, "SO"), /*#__PURE__*/React.createElement(ChannelChip, {
    ch: norm(soCh)
  }), /*#__PURE__*/React.createElement("span", null, "\uD83D\uDCCD ", norm(soSt), norm(soCity) ? ' · ' + norm(soCity) : ''), /*#__PURE__*/React.createElement("span", {
    className: "muted"
  }, "\u2192"), /*#__PURE__*/React.createElement(Avatar, {
    name: soAsm,
    size: 22
  }), /*#__PURE__*/React.createElement("b", null, soAsm))), /*#__PURE__*/React.createElement("div", {
    style: {
      margin: '24px 2px 12px'
    }
  }, /*#__PURE__*/React.createElement("b", {
    style: {
      fontSize: 14
    }
  }, "Sales Officers ", /*#__PURE__*/React.createElement("span", {
    className: "muted"
  }, "(", sos.length, ")"))), sos.length === 0 ? /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      fontSize: 13.5,
      padding: '8px 2px'
    }
  }, "No SOs yet \u2014 click ", /*#__PURE__*/React.createElement("b", null, "\u2795 Add SO"), " above. Each SO reports to the ASM who owns its channel + state, and also shows under that territory in the ", /*#__PURE__*/React.createElement("b", null, "ASM"), " view.") : /*#__PURE__*/React.createElement("div", {
    className: "map-grid"
  }, Object.keys(soByAsm).sort().map(asm => /*#__PURE__*/React.createElement("div", {
    className: "map-card",
    key: asm
  }, /*#__PURE__*/React.createElement("div", {
    className: "map-card-hd"
  }, /*#__PURE__*/React.createElement(Avatar, {
    name: asm,
    size: 30
  }), /*#__PURE__*/React.createElement("b", null, asm), /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontSize: 10.5,
      fontWeight: 800
    }
  }, "ASM"), /*#__PURE__*/React.createElement("span", {
    className: "count"
  }, soByAsm[asm].length, " SO")), /*#__PURE__*/React.createElement("div", {
    className: "map-pills"
  }, soByAsm[asm].map((s, i) => /*#__PURE__*/React.createElement("div", {
    className: "map-pill",
    key: i
  }, /*#__PURE__*/React.createElement("span", {
    className: "mini-av",
    style: {
      background: hashColor(s.so)
    }
  }, initials(s.so)), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      flexDirection: 'column',
      flex: 1,
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("span", {
    className: "st"
  }, s.so), /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontSize: 11.5,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
      textOverflow: 'ellipsis'
    }
  }, /*#__PURE__*/React.createElement("b", {
    style: {
      fontWeight: 700
    }
  }, s.channel), " \xB7 ", s.state, s.city ? ' · ' + s.city : '')), /*#__PURE__*/React.createElement("button", {
    className: "pill-x",
    title: "Remove SO",
    onClick: () => removeSO(s.channel, s.state, s.so, s.city)
  }, "\xD7")))))))), view === 'asm' && /*#__PURE__*/React.createElement("div", {
    className: "map-grid"
  }, groups.map(g => /*#__PURE__*/React.createElement("div", {
    className: "map-card",
    key: g.key,
    onDragOver: g.head === 'channel' && editMode ? e => {
      e.preventDefault();
      if (drag && drag.channel !== g.title) setDragOver(g.title);
    } : undefined,
    onDragLeave: g.head === 'channel' && editMode ? () => setDragOver(o => o === g.title ? null : o) : undefined,
    onDrop: g.head === 'channel' && editMode ? () => {
      if (drag && drag.channel !== g.title) moveTerritory(drag.person, drag.channel, g.title, drag.state);
      setDrag(null);
      setDragOver(null);
    } : undefined,
    style: dragOver === g.title && drag && drag.channel !== g.title ? {
      borderColor: '#16a34a',
      boxShadow: '0 0 0 3px ' + hexToRgba('#16a34a', .18),
      background: '#f0fdf4'
    } : null
  }, /*#__PURE__*/React.createElement("div", {
    className: "map-card-hd"
  }, g.head === 'person' ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(Avatar, {
    name: g.title,
    size: 30
  }), /*#__PURE__*/React.createElement("b", null, g.title), /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontSize: 10.5,
      fontWeight: 800
    }
  }, "ASM")) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(ChannelChip, {
    ch: g.title
  }), /*#__PURE__*/React.createElement("b", null, channelMeta(g.title).full)), /*#__PURE__*/React.createElement("span", {
    className: "count"
  }, g.items.length), (g.head === 'person' || editMode) && /*#__PURE__*/React.createElement("button", {
    className: "card-add",
    title: g.head === 'person' ? 'Add territory to ' + g.title : 'Add ASM to ' + g.title,
    onClick: () => setAddTo({
      mode: g.head === 'person' ? 'asm' : 'channel',
      value: g.title
    })
  }, "+")), /*#__PURE__*/React.createElement("div", {
    className: "map-pills"
  }, g.items.map(r => {
    const t = tSum(targets[keyOf(r.channel, r.state)]);
    if (g.head === 'channel') {
      const un = !r.person; // unassigned slot — state stays fixed, no owner
      const rk = keyOf(r.channel, r.state);
      const isTarget = drag && !(drag.channel === r.channel && drag.state === r.state); // valid swap/fill target
      const hot = dragOverRow === rk && isTarget;
      const selected = editMode && sel.includes(rk);
      return /*#__PURE__*/React.createElement("div", {
        className: "map-pill",
        key: rk,
        draggable: editMode && !un,
        onDragStart: editMode && !un ? () => setDrag({
          person: r.person,
          channel: r.channel,
          state: r.state
        }) : undefined,
        onDragEnd: () => {
          setDrag(null);
          setDragOver(null);
          setDragOverRow(null);
        },
        onClick: un ? () => setAddTo({
          mode: 'fill',
          value: r.channel,
          state: r.state
        }) : editMode ? () => toggleSel(rk) : undefined,
        onDragOver: editMode && isTarget ? e => {
          e.preventDefault();
          e.stopPropagation();
          setDragOverRow(rk);
          setDragOver(null);
        } : undefined,
        onDragLeave: editMode && isTarget ? e => {
          e.stopPropagation();
          setDragOverRow(o => o === rk ? null : o);
        } : undefined,
        onDrop: editMode && isTarget ? e => {
          e.stopPropagation();
          swapOwners(drag.channel, drag.state, r.channel, r.state);
          setDrag(null);
          setDragOver(null);
          setDragOverRow(null);
        } : undefined,
        title: un ? 'Click to assign an ASM to this state' : undefined,
        style: {
          cursor: un || editMode ? 'pointer' : 'default',
          opacity: drag && drag.channel === r.channel && drag.state === r.state ? .4 : 1,
          ...(selected ? {
            background: '#eef2ff',
            border: '1.5px solid #4f46e5',
            boxShadow: '0 0 0 3px ' + hexToRgba('#4f46e5', .18)
          } : hot ? {
            background: '#ecfdf5',
            border: '1px solid #16a34a',
            boxShadow: '0 0 0 2px ' + hexToRgba('#16a34a', .18)
          } : {
            background: un ? '#fffbeb' : undefined,
            border: un ? '1px dashed #fcd34d' : undefined
          })
        }
      }, /*#__PURE__*/React.createElement("div", {
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: 9,
          flex: 1,
          minWidth: 0
        }
      }, editMode && /*#__PURE__*/React.createElement("span", {
        style: {
          width: 18,
          height: 18,
          borderRadius: 5,
          border: '1.5px solid ' + (selected ? '#4f46e5' : '#cbd5e1'),
          background: selected ? '#4f46e5' : '#fff',
          color: '#fff',
          fontSize: 10,
          fontWeight: 800,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0
        }
      }, selected ? sel.indexOf(rk) + 1 : ''), un ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
        className: "mini-av",
        style: {
          background: '#cbd5e1'
        }
      }, "\u2014"), /*#__PURE__*/React.createElement("span", {
        className: "st",
        style: {
          fontStyle: 'italic',
          color: 'var(--tx3)'
        }
      }, "Unassigned")) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
        className: "mini-av",
        style: {
          background: hashColor(r.person)
        }
      }, initials(r.person)), /*#__PURE__*/React.createElement("span", {
        className: "st"
      }, r.person)), /*#__PURE__*/React.createElement("span", {
        className: "muted",
        style: {
          fontSize: 12,
          whiteSpace: 'nowrap'
        }
      }, "\xB7 ", r.state)), !un && t > 0 && /*#__PURE__*/React.createElement("span", {
        className: "muted",
        style: {
          fontSize: 11.5,
          fontWeight: 700,
          whiteSpace: 'nowrap'
        }
      }, fmtL(t)), editMode && /*#__PURE__*/React.createElement("button", {
        className: "pill-x",
        title: un ? 'Remove empty slot' : 'Remove',
        onClick: e => {
          e.stopPropagation();
          removeRow(r.channel, r.state);
        }
      }, "\xD7"));
    }
    // By ASM — expandable territory with SO (city-level) management
    const k = keyOf(r.channel, r.state);
    const list = sosFor(r.channel, r.state);
    const open = exp[k];
    return /*#__PURE__*/React.createElement("div", {
      className: "terr",
      key: k
    }, /*#__PURE__*/React.createElement("div", {
      className: "terr-hd",
      onClick: () => toggleExp(k)
    }, /*#__PURE__*/React.createElement("span", {
      className: "chev",
      style: open ? {
        transform: 'rotate(90deg)'
      } : null
    }, "\u25B6"), /*#__PURE__*/React.createElement(ChannelChip, {
      ch: r.channel
    }), /*#__PURE__*/React.createElement("span", {
      className: "st",
      style: {
        flex: 1
      }
    }, r.state), t > 0 && /*#__PURE__*/React.createElement("span", {
      className: "muted",
      style: {
        fontSize: 11.5,
        fontWeight: 700
      }
    }, fmtL(t)), /*#__PURE__*/React.createElement("span", {
      className: "so-badge"
    }, list.length, " SO"), /*#__PURE__*/React.createElement("button", {
      className: "pill-x",
      title: "Remove territory",
      onClick: e => {
        e.stopPropagation();
        removeRow(r.channel, r.state);
      }
    }, "\xD7")), open && /*#__PURE__*/React.createElement("div", {
      className: "so-wrap"
    }, list.length > 0 ? /*#__PURE__*/React.createElement("div", {
      className: "so-chips"
    }, list.map((s, i) => /*#__PURE__*/React.createElement("span", {
      className: "so-chip",
      key: i
    }, /*#__PURE__*/React.createElement("span", {
      className: "mini-av",
      style: {
        background: hashColor(s.so),
        width: 18,
        height: 18,
        fontSize: 8
      }
    }, initials(s.so)), s.so, /*#__PURE__*/React.createElement("span", {
      className: "muted",
      style: {
        fontWeight: 600
      }
    }, s.city ? ' · ' + s.city : ''), /*#__PURE__*/React.createElement("button", {
      className: "so-x",
      title: "Remove SO",
      onClick: () => removeSO(r.channel, r.state, s.so, s.city)
    }, "\xD7")))) : /*#__PURE__*/React.createElement("div", {
      className: "muted",
      style: {
        fontSize: 12.5,
        padding: '2px 0 4px'
      }
    }, "No SOs yet \u2014 add them in the ", /*#__PURE__*/React.createElement("b", null, "SO"), " tab.")));
  }))))), addTo && /*#__PURE__*/React.createElement("div", {
    className: "modal-overlay",
    onClick: closeAdd
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal",
    style: {
      maxWidth: 580
    },
    onClick: e => e.stopPropagation()
  }, /*#__PURE__*/React.createElement("div", {
    className: "modal-hd"
  }, /*#__PURE__*/React.createElement("h3", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 8
    }
  }, addTo.mode === 'fill' ? '🎯 Assign' : '➕ Add to', addTo.mode === 'asm' ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(Avatar, {
    name: addTo.value,
    size: 22
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 15
    }
  }, addTo.value), /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontWeight: 800,
      fontSize: 10.5
    }
  }, "ASM")) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(ChannelChip, {
    ch: addTo.value
  }), /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontWeight: 600,
      fontSize: 13
    }
  }, addTo.mode === 'fill' ? '· ' + addTo.state : channelMeta(addTo.value).full))), /*#__PURE__*/React.createElement("button", {
    className: "modal-x",
    onClick: closeAdd
  }, "\xD7")), /*#__PURE__*/React.createElement("div", {
    className: "modal-body"
  }, addTo.mode === 'channel' || addTo.mode === 'fill' ? /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, addTo.mode === 'fill' ? 'Pick an ASM for this slot' : 'Step 1 · ASM', " ", customAp && /*#__PURE__*/React.createElement("span", {
    className: "new-badge"
  }, "\u2726 NEW")), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick",
    style: {
      marginBottom: 18
    }
  }, people.map(p => /*#__PURE__*/React.createElement("button", {
    key: p,
    className: "pick",
    style: norm(ap) === p ? PICK_ON : null,
    onClick: () => setAp(p)
  }, /*#__PURE__*/React.createElement(Avatar, {
    name: p,
    size: 18
  }), p)), customAp && /*#__PURE__*/React.createElement("button", {
    className: "pick",
    style: greenOn,
    onClick: () => setAp('')
  }, ap, " \u2715"), aNew ? /*#__PURE__*/React.createElement("input", {
    autoFocus: true,
    className: "pick-input",
    value: aDraft,
    placeholder: "New ASM\u2026",
    onChange: e => setADraft(e.target.value),
    onKeyDown: e => {
      if (e.key === 'Enter') commitNew();
      if (e.key === 'Escape') {
        setANew(false);
        setADraft('');
      }
    },
    onBlur: commitNew
  }) : /*#__PURE__*/React.createElement("button", {
    className: "pick add",
    onClick: () => setANew(true)
  }, "+ New"))) : /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "Step 1 \xB7 Channel"), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick",
    style: {
      marginBottom: 18
    }
  }, (showAllCh ? ALL_CHANNELS : PRIORITY_CHANNELS).map(c => /*#__PURE__*/React.createElement("button", {
    key: c,
    className: "pick",
    style: norm(ac) === c ? chStyle(c) : null,
    onClick: () => setAc(c)
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      width: 9,
      height: 9,
      borderRadius: 3,
      background: channelMeta(c).color,
      display: 'inline-block'
    }
  }), c)), !showAllCh && /*#__PURE__*/React.createElement("button", {
    className: "pick more",
    onClick: () => setShowAllCh(true)
  }, "\u22EF Show all (", ALL_CHANNELS.length, ")"))), addTo.mode !== 'fill' && /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
    className: "lbl"
  }, "Step 2 \xB7 State"), /*#__PURE__*/React.createElement("div", {
    className: "chip-pick"
  }, (showAll ? allStates : PRIORITY_STATES).map(s => /*#__PURE__*/React.createElement("button", {
    key: s,
    className: "pick",
    style: norm(ast) === s ? PICK_ON : null,
    onClick: () => setAst(s)
  }, s)), !showAll && /*#__PURE__*/React.createElement("button", {
    className: "pick more",
    onClick: () => setShowAll(true)
  }, "\u22EF Show all (", allStates.length, ")")))), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: '14px 22px',
      borderTop: '1px solid var(--border)',
      display: 'flex',
      gap: 10,
      alignItems: 'center'
    }
  }, canAdd && /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      marginRight: 'auto',
      fontSize: 13,
      fontWeight: 600
    }
  }, addSummary), /*#__PURE__*/React.createElement("button", {
    className: "btn",
    style: canAdd ? null : {
      marginLeft: 'auto'
    },
    onClick: closeAdd
  }, "Cancel"), /*#__PURE__*/React.createElement("button", {
    className: "btn btn-green",
    disabled: !canAdd,
    onClick: doAdd
  }, "+ Add")))));
}

/* Product master manager — Premium / Commodity lists, add & remove. */
function ProductManager({
  products,
  addProduct,
  removeProduct
}) {
  const [draft, setDraft] = useState({
    P: '',
    C: ''
  });
  const add = type => {
    const v = draft[type].trim();
    if (v) {
      addProduct(v, type);
      setDraft(d => ({
        ...d,
        [type]: ''
      }));
    }
  };
  const section = (type, label, color) => {
    const list = products.filter(p => p.type === type);
    return /*#__PURE__*/React.createElement("div", {
      className: "pick-field"
    }, /*#__PURE__*/React.createElement("span", {
      className: "lbl",
      style: {
        color
      }
    }, label, " ", /*#__PURE__*/React.createElement("span", {
      className: "muted"
    }, "(", list.length, ")")), /*#__PURE__*/React.createElement("div", {
      className: "chip-pick"
    }, list.map(p => /*#__PURE__*/React.createElement("span", {
      key: p.name,
      className: "pick",
      style: {
        cursor: 'default'
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        width: 8,
        height: 8,
        borderRadius: 3,
        background: color,
        display: 'inline-block'
      }
    }), p.name, /*#__PURE__*/React.createElement("button", {
      className: "so-x",
      title: "Remove",
      onClick: () => removeProduct(p.name, type)
    }, "\xD7"))), /*#__PURE__*/React.createElement("span", {
      style: {
        display: 'inline-flex',
        gap: 6,
        alignItems: 'center'
      }
    }, /*#__PURE__*/React.createElement("input", {
      className: "so-input",
      placeholder: 'New ' + label.toLowerCase() + '…',
      value: draft[type],
      onChange: e => setDraft(d => ({
        ...d,
        [type]: e.target.value
      })),
      onKeyDown: e => {
        if (e.key === 'Enter') add(type);
      }
    }), /*#__PURE__*/React.createElement("button", {
      className: "btn btn-green",
      style: {
        padding: '7px 13px',
        fontSize: 12.5
      },
      disabled: !draft[type].trim(),
      onClick: () => add(type)
    }, "+ Add"))));
  };
  return /*#__PURE__*/React.createElement("div", {
    className: "add-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "panel-hint",
    style: {
      marginBottom: 14
    }
  }, "\uD83D\uDCE6 The ", /*#__PURE__*/React.createElement("b", null, "product master"), " \u2014 these appear when you set product-level targets in the Data View. (A product can sit in both lists, e.g. ", /*#__PURE__*/React.createElement("b", null, "Blended"), ".)"), section('P', 'Premium', '#0d9488'), section('C', 'Commodity', '#d97706'));
}

/* ─── TAB 6 — Manage / Add (the data-management screen) ────────────────────── */
function ManageData({
  rows,
  channels,
  addRow,
  removeRow,
  targets,
  sos,
  addSO,
  removeSO,
  moveTerritory,
  swapOwners,
  undo,
  histLen,
  restoreSeed,
  products,
  addProduct,
  removeProduct
}) {
  const [form, setForm] = useState({
    person: '',
    channel: '',
    state: ''
  });
  const [view, setView] = useState('asm'); // top-right master toggle: ASM territory mgmt | SO mgmt
  const [showAdd, setShowAdd] = useState(false); // the add form is collapsed by default
  const people = distinct(rows.map(r => r.person)).sort();
  const states = distinct(rows.map(r => r.state)).sort();
  const isNew = (val, opts) => norm(val) && !opts.map(norm).includes(norm(val));
  const ready = form.person.trim() && form.channel.trim() && form.state.trim();
  // Only warn about reassignment once a STATE is picked — otherwise an empty state
  // matches the channel-level (national) cell and shows a misleading owner.
  const dupOwner = (() => {
    if (!norm(form.state)) return null;
    const r = rows.find(x => x.channel === norm(form.channel) && x.state === norm(form.state));
    return r ? r.person : null;
  })();
  const submit = () => {
    if (!ready) return;
    addRow(form);
    setForm({
      person: '',
      channel: '',
      state: ''
    });
  };
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      justifyContent: 'flex-start',
      alignItems: 'center',
      gap: 10,
      flexWrap: 'wrap',
      marginBottom: showAdd ? 10 : 8
    }
  }, /*#__PURE__*/React.createElement("div", {
    className: "seg"
  }, /*#__PURE__*/React.createElement("button", {
    className: 'seg-b' + (view === 'asm' ? ' on' : ''),
    onClick: () => {
      setView('asm');
      setShowAdd(false);
    }
  }, "\uD83D\uDC64 ASM"), /*#__PURE__*/React.createElement("button", {
    className: 'seg-b' + (view === 'so' ? ' on' : ''),
    onClick: () => {
      setView('so');
      setShowAdd(false);
    }
  }, "\uD83E\uDDD1\u200D\uD83D\uDCBC SO")), /*#__PURE__*/React.createElement("button", {
    className: "btn btn-green",
    onClick: () => setShowAdd(s => !s)
  }, showAdd ? '✕ Close' : view === 'asm' ? '➕ Add ASM' : '➕ Add SO')), view === 'asm' && showAdd && /*#__PURE__*/React.createElement("div", {
    className: "add-card"
  }, /*#__PURE__*/React.createElement(ChipPicker, {
    label: "ASM (Area Sales Manager)",
    value: form.person,
    onChange: v => setForm({
      ...form,
      person: v
    }),
    options: people,
    icon: o => /*#__PURE__*/React.createElement(Avatar, {
      name: o,
      size: 18
    }),
    addLabel: "ASM"
  }), /*#__PURE__*/React.createElement(ChannelPicker, {
    value: form.channel,
    onChange: v => setForm({
      ...form,
      channel: v
    })
  }), /*#__PURE__*/React.createElement(StatePicker, {
    value: form.state,
    onChange: v => setForm({
      ...form,
      state: v
    })
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 10,
      alignItems: 'center',
      marginTop: 8
    }
  }, /*#__PURE__*/React.createElement("button", {
    className: "btn btn-green",
    style: {
      padding: '12px 24px',
      fontSize: 14
    },
    disabled: !ready,
    onClick: submit
  }, "+ Add territory"), /*#__PURE__*/React.createElement("button", {
    className: "btn",
    disabled: !(form.person || form.channel || form.state),
    onClick: () => setForm({
      person: '',
      channel: '',
      state: ''
    })
  }, "Clear selection")), ready && /*#__PURE__*/React.createElement("div", {
    className: "preview"
  }, /*#__PURE__*/React.createElement("span", {
    className: "muted",
    style: {
      fontSize: 12,
      fontWeight: 700
    }
  }, "PREVIEW"), /*#__PURE__*/React.createElement(Avatar, {
    name: norm(form.person),
    size: 28
  }), /*#__PURE__*/React.createElement("b", null, norm(form.person)), /*#__PURE__*/React.createElement(ChannelChip, {
    ch: norm(form.channel)
  }), /*#__PURE__*/React.createElement("span", null, "\uD83D\uDCCD ", norm(form.state))), dupOwner && dupOwner !== norm(form.person) && /*#__PURE__*/React.createElement("div", {
    className: "warn"
  }, "\u26A0\uFE0F ", norm(form.channel), " \xB7 ", norm(form.state), " is currently owned by ", /*#__PURE__*/React.createElement("b", {
    style: {
      margin: '0 4px'
    }
  }, dupOwner), " \u2014 adding will reassign it to ", norm(form.person), ".")), /*#__PURE__*/React.createElement(MappingCards, {
    rows: rows,
    channels: channels,
    targets: targets,
    removeRow: removeRow,
    addRow: addRow,
    sos: sos,
    addSO: addSO,
    removeSO: removeSO,
    moveTerritory: moveTerritory,
    swapOwners: swapOwners,
    undo: undo,
    histLen: histLen,
    restoreSeed: restoreSeed,
    showAdd: showAdd,
    view: view
  }));
}

/* ─── APP shell ───────────────────────────────────────────────────────────── */
const TABS = [{
  id: 'cards',
  label: '📊 Data View'
}];
function App() {
  const BOOT = typeof window !== 'undefined' && window.__PT_BOOT__ || {
    urls: {},
    csrf: '',
    month: 1,
    year: 2026,
    isAdmin: false,
    monthOptions: [],
    yearOptions: []
  };
  const admin = !!BOOT.isAdmin;
  const jhead = {
    'Content-Type': 'application/json',
    'X-CSRFToken': BOOT.csrf
  };
  const monthLabel = m => {
    const o = (BOOT.monthOptions || []).find(x => x[0] === m);
    return o ? o[1] : m;
  };
  const [tab, setTab] = useState('cards');
  const [rows, setRows] = useState([]); // [{person,channel,state}] — grid
  const [targets, setTargets] = useState({}); // {channel||state: {P#sub:{l,r}}}
  const [sos, setSos] = useState([]); // sales officers (client-side only)
  const [products, setProducts] = useState(PRODUCT_SEED);
  const [month, setMonth] = useState(BOOT.month);
  const [year, setYear] = useState(BOOT.year);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(''); // '', 'targets','mapping','refresh'
  const [dirtyT, setDirtyT] = useState(false);
  const [dirtyM, setDirtyM] = useState(false);
  const [chRows, setChRows] = useState([]); // channel-target rows: last-month sale + current target
  const [chLast, setChLast] = useState(null); // {month,year} of the last-month sale shown
  const [dirtyCh, setDirtyCh] = useState(false);
  const [note, setNote] = useState(null); // {kind:'ok'|'err', text}
  const flash = (kind, text) => setNote({
    kind,
    text
  });
  const loadMap = useCallback(async () => {
    const r = await fetch(BOOT.urls.map, {
      headers: {
        'X-CSRFToken': BOOT.csrf
      }
    });
    const d = await r.json();
    setRows((d.cells || []).map(c => ({
      person: c.sales_person || '',
      channel: c.channel,
      state: c.state_name
    })));
    setDirtyM(false);
  }, []);
  const loadTargets = useCallback(async (m, y) => {
    const r = await fetch(BOOT.urls.productTargets + '?month=' + m + '&year=' + y, {
      headers: {
        'X-CSRFToken': BOOT.csrf
      }
    });
    const d = await r.json();
    setTargets(d.data || {});
    if (Array.isArray(d.products) && d.products.length) setProducts(d.products);
    setDirtyT(false);
  }, []);
  const loadChannelTargets = useCallback(async (m, y) => {
    if (!BOOT.urls.channelTargets) return;
    const r = await fetch(BOOT.urls.channelTargets + '?month=' + m + '&year=' + y, {
      headers: {
        'X-CSRFToken': BOOT.csrf
      }
    });
    const d = await r.json();
    setChRows(d.rows || []);
    setChLast({
      month: d.last_month,
      year: d.last_year
    });
    setDirtyCh(false);
  }, []);
  // Edit one channel's Premium/Commodity target field; '_uselast' copies last month's
  // Premium & Commodity sale into the respective targets.
  const setChField = (ch, field, val) => {
    setChRows(rs => rs.map(r => {
      if (r.channel !== ch) return r;
      if (field === '_uselast') return {
        ...r,
        premium_ltrs: Math.round(r.last_premium_ltrs || 0),
        premium_realise: r.last_premium_realise || 0,
        commodity_ltrs: Math.round(r.last_commodity_ltrs || 0),
        commodity_realise: r.last_commodity_realise || 0
      };
      return {
        ...r,
        [field]: val
      };
    }));
    setDirtyCh(true);
  };
  const saveChannelTargets = async () => {
    const items = chRows.map(r => ({
      channel: r.channel,
      premium_ltrs: parseFloat(r.premium_ltrs) || 0,
      premium_realise: parseFloat(r.premium_realise) || 0,
      commodity_ltrs: parseFloat(r.commodity_ltrs) || 0,
      commodity_realise: parseFloat(r.commodity_realise) || 0
    }));
    const res = await fetch(BOOT.urls.channelTargetsSave, {
      method: 'POST',
      headers: jhead,
      body: JSON.stringify({
        month,
        year,
        targets: items
      })
    });
    const d = await res.json();
    if (d.status !== 'ok') throw new Error(d.error || 'channel save failed');
    setDirtyCh(false);
  };
  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        await loadMap();
        await loadTargets(BOOT.month, BOOT.year);
        await loadChannelTargets(BOOT.month, BOOT.year);
      } catch (e) {
        flash('err', 'Load failed: ' + e.message);
      }
      setLoading(false);
    })();
  }, []);
  useEffect(() => {
    if (!loading) {
      loadTargets(month, year).catch(() => flash('err', 'Could not load targets'));
      loadChannelTargets(month, year).catch(() => {});
    }
  }, [month, year]);
  const channels = useMemo(() => orderChannels(rows), [rows]);

  // ── Undo history ──
  const stateRef = useRef({
    rows,
    sos,
    targets
  });
  stateRef.current = {
    rows,
    sos,
    targets
  };
  const histRef = useRef([]);
  const [histLen, setHistLen] = useState(0);
  const snapshot = useCallback(() => {
    histRef.current.push(stateRef.current);
    if (histRef.current.length > 60) histRef.current.shift();
    setHistLen(histRef.current.length);
  }, []);
  const undo = useCallback(() => {
    const prev = histRef.current.pop();
    if (prev) {
      setRows(prev.rows);
      setTargets(prev.targets);
      setSos(prev.sos);
      setDirtyM(true);
      setDirtyT(true);
    }
    setHistLen(histRef.current.length);
  }, []);
  const setTarget = useCallback((channel, state, productId, field, val) => {
    setTargets(t => {
      const k = keyOf(channel, state);
      const legacy = t[k] && typeof t[k] === 'object' && ('p' in t[k] || 'c' in t[k]);
      const cur = t[k] && typeof t[k] === 'object' && !legacy ? t[k] : {};
      const cp0 = cur[productId] && typeof cur[productId] === 'object' ? cur[productId] : {
        l: +cur[productId] || 0,
        r: 0
      };
      const np = {
        ...cp0,
        [field]: val === '' ? 0 : Number(val)
      };
      const next = {
        ...cur
      };
      if (!+np.l && !+np.r) delete next[productId];else next[productId] = np;
      if (Object.keys(next).length === 0) {
        const x = {
          ...t
        };
        delete x[k];
        return x;
      }
      return {
        ...t,
        [k]: next
      };
    });
    setDirtyT(true);
  }, []);
  // Clear ALL product targets for one (channel,state) territory.
  const clearCell = useCallback((channel, state) => {
    setTargets(t => {
      const k = keyOf(channel, state);
      if (!(k in t)) return t;
      const x = {
        ...t
      };
      delete x[k];
      return x;
    });
    setDirtyT(true);
  }, []);
  // Person-only reassignment (existing cell) or add a new (channel,state) cell.
  const addRow = useCallback(f => {
    const person = norm(f.person),
      channel = norm(f.channel),
      state = norm(f.state);
    if (!channel || !state) return;
    snapshot();
    setDirtyM(true);
    setRows(rs => {
      const i = rs.findIndex(r => r.channel === channel && r.state === state);
      if (i >= 0) {
        const cp = [...rs];
        cp[i] = {
          ...cp[i],
          person
        };
        return cp;
      }
      return [...rs, {
        person,
        channel,
        state
      }];
    });
  }, []);
  const removeRow = useCallback((channel, state) => {
    // unassign (keep fixed cell)
    snapshot();
    setDirtyM(true);
    setRows(rs => rs.map(r => r.channel === channel && r.state === state ? {
      ...r,
      person: ''
    } : r));
    setTargets(t => {
      const c = {
        ...t
      };
      delete c[keyOf(channel, state)];
      return c;
    });
    setSos(ss => ss.filter(s => !(s.channel === channel && s.state === state)));
  }, []);
  const addSO = useCallback((channel, state, so, city) => {
    const C = norm(channel),
      S = norm(state),
      SO = norm(so),
      CT = norm(city);
    if (!C || !S || !SO) return;
    snapshot();
    setSos(ss => ss.some(x => x.channel === C && x.state === S && x.so === SO && x.city === CT) ? ss : [...ss, {
      channel: C,
      state: S,
      so: SO,
      city: CT
    }]);
  }, []);
  const removeSO = useCallback((channel, state, so, city) => {
    snapshot();
    setSos(ss => ss.filter(s => !(s.channel === channel && s.state === state && s.so === so && s.city === city)));
  }, []);
  const addProduct = useCallback((name, type) => {
    const N = norm(name),
      T = type === 'P' ? 'P' : 'C';
    if (!N) return;
    setProducts(ps => ps.some(p => p.name === N && p.type === T) ? ps : [...ps, {
      name: N,
      type: T
    }]);
  }, []);
  const removeProduct = useCallback((name, type) => {
    setProducts(ps => ps.filter(p => !(p.name === name && p.type === type)));
  }, []);
  const moveTerritory = useCallback((person, fromCh, toCh, state) => {
    const F = norm(fromCh),
      T = norm(toCh),
      P = norm(person),
      S = norm(state);
    if (!T || F === T) return;
    const existing = rows.find(r => r.channel === T && r.state === S);
    if (existing && existing.person && existing.person !== P && !confirm(`${T} · ${S} is already owned by ${existing.person}.\nMove ${P} here and replace them?`)) return;
    snapshot();
    setDirtyM(true);
    setRows(rs => {
      const cleared = rs.filter(r => !(r.channel === T && r.state === S));
      const freed = cleared.map(r => r.channel === F && r.state === S ? {
        ...r,
        person: ''
      } : r);
      return [...freed, {
        person: P,
        channel: T,
        state: S
      }];
    });
    setTargets(t => {
      const c = {
        ...t
      };
      const v = c[keyOf(F, S)];
      delete c[keyOf(F, S)];
      if (v !== undefined) c[keyOf(T, S)] = v;
      return c;
    });
    setSos(ss => {
      const out = [];
      ss.forEach(s => {
        const ns = s.channel === F && s.state === S ? {
          ...s,
          channel: T
        } : s;
        if (!out.some(x => x.channel === ns.channel && x.state === ns.state && x.so === ns.so && x.city === ns.city)) out.push(ns);
      });
      return out;
    });
  }, [rows]);
  const swapOwners = useCallback((aCh, aState, bCh, bState) => {
    const A = norm(aCh),
      AS = norm(aState),
      B = norm(bCh),
      BS = norm(bState);
    if (A === B && AS === BS) return;
    snapshot();
    setDirtyM(true);
    setRows(rs => {
      const a = rs.find(r => r.channel === A && r.state === AS),
        b = rs.find(r => r.channel === B && r.state === BS);
      const pa = a ? a.person : '',
        pb = b ? b.person : '';
      return rs.map(r => r.channel === A && r.state === AS ? {
        ...r,
        person: pb
      } : r.channel === B && r.state === BS ? {
        ...r,
        person: pa
      } : r);
    });
  }, []);
  const refreshFromSap = async () => {
    if (!admin) return;
    setBusy('refresh');
    setNote(null);
    try {
      const res = await fetch(BOOT.urls.refresh, {
        method: 'POST',
        headers: jhead,
        body: '{}'
      });
      const d = await res.json();
      if (d.status === 'ok') {
        await loadMap();
        flash('ok', 'Grid refreshed from SAP — ' + (d.cells ? d.cells.length : '') + ' cells.');
      } else flash('err', 'Refresh failed: ' + (d.error || res.status));
    } catch (e) {
      flash('err', 'Refresh failed: ' + e.message);
    }
    setBusy('');
  };
  const restoreSeed = () => {
    if (admin && confirm('Re-sync the grid from SAP? Channel/state cells refresh; assignments are kept.')) {
      refreshFromSap();
    }
  };
  const totalsByChannel = useMemo(() => {
    const o = {};
    rows.forEach(r => {
      o[r.channel] = (o[r.channel] || 0) + tSum(targets[keyOf(r.channel, r.state)]);
    });
    return o;
  }, [rows, targets]);
  const grand = Object.values(totalsByChannel).reduce((a, b) => a + b, 0);
  const grandP = rows.reduce((s, r) => s + tByType(targets[keyOf(r.channel, r.state)], 'P'), 0);
  const grandC = rows.reduce((s, r) => s + tByType(targets[keyOf(r.channel, r.state)], 'C'), 0);
  const assignedCount = rows.filter(r => tSum(targets[keyOf(r.channel, r.state)]) > 0).length;

  // Data View save: channel-level targets (cards) + per-product targets (state modal),
  // whichever are dirty — both reflect on the dashboard.
  const saveTargets = async () => {
    if (!admin) return;
    setBusy('targets');
    setNote(null);
    try {
      const parts = [];
      if (dirtyCh) {
        await saveChannelTargets();
        parts.push('channel targets');
      }
      if (dirtyT) {
        const res = await fetch(BOOT.urls.productTargetsSave, {
          method: 'POST',
          headers: jhead,
          body: JSON.stringify({
            month,
            year,
            targets
          })
        });
        const d = await res.json();
        if (d.status !== 'ok') throw new Error(d.error || res.status);
        setDirtyT(false);
        parts.push(d.saved + ' product rows');
      }
      flash('ok', 'Saved ' + (parts.join(' + ') || 'targets') + ' — dashboard updated (' + monthLabel(month) + ' ' + year + ').');
    } catch (e) {
      flash('err', 'Save failed: ' + e.message);
    }
    setBusy('');
  };
  const saveMapping = async () => {
    if (!admin) return;
    setBusy('mapping');
    setNote(null);
    try {
      const assignments = rows.map(r => ({
        channel: r.channel,
        state_name: r.state,
        sales_person: r.person
      }));
      const res = await fetch(BOOT.urls.mapSave, {
        method: 'POST',
        headers: jhead,
        body: JSON.stringify({
          assignments
        })
      });
      const d = await res.json();
      if (d.status === 'ok') {
        setDirtyM(false);
        await loadMap();
        flash('ok', 'Mapping saved — ' + d.saved + ' updated.');
      } else flash('err', 'Save failed: ' + (d.error || res.status));
    } catch (e) {
      flash('err', 'Save failed: ' + e.message);
    }
    setBusy('');
  };
  const exportCSV = () => {
    const head = 'Salesperson,Channel,State,Premium Ltrs,Commodity Ltrs,Total Ltrs\n';
    const body = rows.map(r => {
      const v = targets[keyOf(r.channel, r.state)];
      return `${r.person},${r.channel},${r.state},${tByType(v, 'P')},${tByType(v, 'C')},${tSum(v)}`;
    }).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([head + body], {
      type: 'text/csv'
    }));
    a.download = 'sales_targets_' + month + '_' + year + '.csv';
    a.click();
  };
  const assignedRows = rows.filter(r => r.person);
  const shared = {
    rows: assignedRows,
    channels,
    targets,
    setTarget,
    clearCell,
    totalsByChannel,
    chRows,
    setChField,
    admin,
    chLast,
    monthLabel,
    products,
    month,
    year,
    BOOT
  };
  const onManage = tab === 'manage';
  return /*#__PURE__*/React.createElement("div", {
    className: "app"
  }, /*#__PURE__*/React.createElement("div", {
    className: "pt-toprow"
  }, /*#__PURE__*/React.createElement("a", {
    className: "pt-back",
    href: BOOT.dashboardUrl || '/realise/#slide2',
    target: "_top"
  }, "\u2190 Back to Dashboard"), /*#__PURE__*/React.createElement("h1", null, "\uD83C\uDFAF Sales Target Assignment")), /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, /*#__PURE__*/React.createElement("div", {
    className: "kpi-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "v"
  }, distinct(rows.map(r => r.person)).filter(Boolean).length), /*#__PURE__*/React.createElement("div", {
    className: "l"
  }, "ASMs")), /*#__PURE__*/React.createElement("div", {
    className: "kpi-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "v"
  }, sos.length), /*#__PURE__*/React.createElement("div", {
    className: "l"
  }, "SOs")), /*#__PURE__*/React.createElement("div", {
    className: "kpi-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "v",
    style: {
      color: '#0d9488'
    }
  }, fmtL(grandP)), /*#__PURE__*/React.createElement("div", {
    className: "l"
  }, "Premium Ltrs")), /*#__PURE__*/React.createElement("div", {
    className: "kpi-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "v",
    style: {
      color: '#d97706'
    }
  }, fmtL(grandC)), /*#__PURE__*/React.createElement("div", {
    className: "l"
  }, "Commodity Ltrs")), /*#__PURE__*/React.createElement("div", {
    className: "kpi-card accent"
  }, /*#__PURE__*/React.createElement("div", {
    className: "v"
  }, fmtL(grand)), /*#__PURE__*/React.createElement("div", {
    className: "l"
  }, "Total Target Ltrs"))), /*#__PURE__*/React.createElement("div", {
    className: "toolbar"
  }, /*#__PURE__*/React.createElement("div", {
    className: "tabs"
  }, TABS.map(t => /*#__PURE__*/React.createElement("button", {
    key: t.id,
    className: 'tab' + (tab === t.id ? ' active' : ''),
    onClick: () => setTab(t.id)
  }, t.label)), /*#__PURE__*/React.createElement("button", {
    className: 'tab add' + (onManage ? ' active' : ''),
    onClick: () => setTab('manage')
  }, "\u2699\uFE0F Manage People")), /*#__PURE__*/React.createElement("div", {
    className: "actions"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lbl",
    style: {
      margin: '0 4px 0 0'
    }
  }, "Period"), /*#__PURE__*/React.createElement("select", {
    className: "sel",
    style: {
      width: 'auto',
      minWidth: 92,
      padding: '8px 28px 8px 11px'
    },
    value: month,
    onChange: e => setMonth(+e.target.value)
  }, (BOOT.monthOptions || []).map(m => /*#__PURE__*/React.createElement("option", {
    key: m[0],
    value: m[0]
  }, m[1]))), /*#__PURE__*/React.createElement("select", {
    className: "sel",
    style: {
      width: 'auto',
      minWidth: 86,
      padding: '8px 28px 8px 11px'
    },
    value: year,
    onChange: e => setYear(+e.target.value)
  }, (BOOT.yearOptions || []).map(y => /*#__PURE__*/React.createElement("option", {
    key: y,
    value: y
  }, y))), /*#__PURE__*/React.createElement("button", {
    className: "btn",
    onClick: exportCSV
  }, "Export CSV"), admin && onManage && /*#__PURE__*/React.createElement("button", {
    className: "btn",
    disabled: busy === 'refresh',
    onClick: refreshFromSap
  }, busy === 'refresh' ? 'Refreshing…' : '↻ Refresh from SAP'), admin && (onManage ? /*#__PURE__*/React.createElement("button", {
    className: "btn btn-green",
    disabled: !dirtyM || busy === 'mapping',
    onClick: saveMapping
  }, busy === 'mapping' ? 'Saving…' : 'Save Mapping') : /*#__PURE__*/React.createElement("button", {
    className: "btn btn-primary",
    disabled: !dirtyT && !dirtyCh || busy === 'targets',
    onClick: saveTargets
  }, busy === 'targets' ? 'Saving…' : 'Save Targets')))), note && /*#__PURE__*/React.createElement("div", {
    className: "warn",
    style: {
      background: note.kind === 'ok' ? 'var(--green-light)' : 'var(--red-light)',
      color: note.kind === 'ok' ? '#047857' : '#b91c1c'
    }
  }, note.kind === 'ok' ? '✓' : '⚠️', " ", note.text), !admin && /*#__PURE__*/React.createElement("div", {
    className: "warn"
  }, "\uD83D\uDC41\uFE0F View-only \u2014 you don't have edit rights."), /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, loading ? /*#__PURE__*/React.createElement("div", {
    className: "muted",
    style: {
      padding: '48px 4px',
      textAlign: 'center',
      fontWeight: 600
    }
  }, "Loading territory map\u2026") : /*#__PURE__*/React.createElement("div", null, tab === 'cards' && /*#__PURE__*/React.createElement(DrillCards, shared), tab === 'manage' && /*#__PURE__*/React.createElement(ManageData, {
    rows: rows,
    channels: channels,
    addRow: addRow,
    removeRow: removeRow,
    targets: targets,
    sos: sos,
    addSO: addSO,
    removeSO: removeSO,
    moveTerritory: moveTerritory,
    swapOwners: swapOwners,
    undo: undo,
    histLen: histLen,
    restoreSeed: restoreSeed,
    products: products,
    addProduct: addProduct,
    removeProduct: removeProduct
  })), /*#__PURE__*/React.createElement("div", {
    className: "foot-note"
  }, onManage ? /*#__PURE__*/React.createElement(React.Fragment, null, "Reassign each territory's ", /*#__PURE__*/React.createElement("b", null, "person"), ", or add a new channel\xD7state. ", /*#__PURE__*/React.createElement("b", null, "Save Mapping"), " persists \u2014 reflows into the dashboard. ", /*#__PURE__*/React.createElement("b", null, "\u21BB Refresh from SAP"), " adds new cells without touching assignments.") : /*#__PURE__*/React.createElement(React.Fragment, null, "On each ", /*#__PURE__*/React.createElement("b", null, "channel card"), " set its ", /*#__PURE__*/React.createElement("b", null, "total target"), " (vs last month's sale), or click a card to set ", /*#__PURE__*/React.createElement("b", null, "per-product targets"), " per state. ", /*#__PURE__*/React.createElement("b", null, "Save Targets"), " pushes both to the Realise dashboard for ", /*#__PURE__*/React.createElement("b", null, monthLabel(month), " ", year), "."))));
}
ReactDOM.createRoot(document.getElementById('root')).render(/*#__PURE__*/React.createElement(App, null));