#let result = json(sys.inputs.at("records"))
#let records = result.at("records")
#let filters = result.at("filters")
#let date-labels = (source: "来源发布日期", event: "活动日期", deadline: "截止日期", publication: "网站刊发日期")
#let relation-labels = (any: "任意学院关系", publisher: "发布学院", associated: "关联学院", audience: "面向学院")
#let literal(value, unknown: "未知") = text(if value == none or value == "" { unknown } else { str(value) })

#set page(paper: "a4", margin: 18mm, numbering: "1 / 1")
#set text(font: ("Noto Sans CJK SC", "DejaVu Sans"), size: 10pt, lang: "zh")
#set par(justify: false, leading: 0.7em)
#set heading(numbering: none)

#align(center)[
  #text(size: 18pt, weight: "bold", result.at("site_name") + " · 查询导出")
  #v(4pt)
  #text(size: 9pt)[个人检索快照 · 可能包含未核验内容 · 不代表正式刊发]
]
#v(6pt)
#text(weight: "bold")[筛选条件]
#parbreak()
#let labels = (q: "关键词", date_type: "日期字段", date_from: "起始日期", date_to: "结束日期", colleges: "学院", college_relation: "学院关系", sources: "来源", tags: "类别")
#for (key, value) in filters.pairs() {
  if value != none and value != "" and value != () {
    let shown = if key == "date_type" { date-labels.at(value, default: value) } else if key == "college_relation" { relation-labels.at(value, default: value) } else if type(value) == array { value.map(str).join("、") } else { str(value) }
    [#literal(labels.at(key, default: key))：#literal(shown) #h(8pt)]
  }
}
#parbreak()
共 #records.len() 条。日期未知时留空标记，不以抓取日期或系统年份替代。
#v(8pt)
#line(length: 100%)

#for (index, record) in records.enumerate() {
  heading(level: 2, text(str(index + 1) + ". " + record.at("title")))
  [来源：#literal(record.at("source_name", default: none)) #h(6pt)
   核验状态：#literal(record.at("verification_status", default: none)) #h(6pt)
   提取状态：#literal(record.at("model_status", default: none))]
  parbreak()
  for (key, label) in (("source_published_date", "来源发布"), ("event_date", "活动日期"), ("deadline_date", "截止日期"), ("publication_date", "网站刊发")) {
    [#literal(label)：#literal(record.at(key, default: none)) #h(8pt)]
  }
  parbreak()
  let colleges = record.at("colleges", default: none)
  if colleges != none and colleges.len() > 0 {
    for college in colleges {
      [#literal(relation-labels.at(college.at("relation"), default: "学院"))：#literal(college.at("name")) #h(8pt)]
    }
    parbreak()
  }
  literal(record.at("description", default: none), unknown: "没有模型摘要，请在网页查阅留存原文。")
  parbreak()
  let uncertainty = record.at("date_uncertainty", default: none)
  if uncertainty != none and uncertainty != "" {
    [#text(weight: "bold")[日期待核对：]#literal(uncertainty)]
    parbreak()
  }
  let url = record.at("source_url", default: none)
  if url != none and url != "" {
    [#link(url)[原文链接]：#text(size: 8pt, url)]
    parbreak()
  }
  v(6pt)
  line(length: 100%, stroke: 0.4pt)
}
