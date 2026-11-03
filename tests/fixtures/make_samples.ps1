# 用真实的 Excel / Word 生成解析器夹具（sample.xlsx / sample.docx）。
#
# 为什么不能用代码手搓 XML：手搓的样本只能证明「解析器认我自己写的东西」，
# 证明不了「Office 存出来的东西解析器认」。这两件事差得很远 —— 合并单元格、
# 隐藏文字、修订痕迹这些，只有真 Office 才会写成那个样子。
#
# 用法（需要本机装了 Office）：
#   powershell -ExecutionPolicy Bypass -File tests\fixtures\make_samples.ps1
#
# 只在解析器改动、需要重新对齐夹具时跑一次；平时不要跑，夹具是入过库的。

$ErrorActionPreference = 'Stop'
$out = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------------------------------------------------------- xlsx
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false
$xl.DisplayAlerts = $false
$wb = $xl.Workbooks.Add()
$ws = $wb.Worksheets.Item(1)
$ws.Name = '主要指标'

$ws.Range('A1:E1').Merge()
$ws.Range('A1').Value2 = '2026年9月主要经济指标'
$ws.Range('A1').Font.Bold = $true
$ws.Range('A2').Value2 = '指标'
$ws.Range('B2').Value2 = '单位'
$ws.Range('C2').Value2 = '本月'
$ws.Range('D2').Value2 = '同比'
$ws.Range('E2').Value2 = '校验列'
$ws.Range('A3').Value2 = '规上工业增加值'
$ws.Range('B3').Value2 = '%'
$ws.Range('C3').NumberFormat = '0.0%'
$ws.Range('C3').Value2 = 0.056
$ws.Range('D3').NumberFormat = '0.0%'
$ws.Range('D3').Value2 = 0.072
$ws.Range('E3').Formula = '=C3*2'
$ws.Range('A4').Value2 = '固定资产投资'
$ws.Range('B4').Value2 = '亿元'
$ws.Range('C4').NumberFormat = '#,##0.0'
$ws.Range('C4').Value2 = 1234.5
$ws.Range('D4').NumberFormat = '0.00'
$ws.Range('D4').Value2 = -3.25
$ws.Range('A5').Value2 = '统计截止'
$ws.Range('C5').NumberFormat = 'yyyy-mm-dd'
$ws.Range('C5').Value2 = [datetime]'2026-09-30'
$ws.Range('A6').Value2 = '数据来源'
$ws.Range('C6').Value2 = '市统计局'

$ws2 = $wb.Worksheets.Add([System.Reflection.Missing]::Value, $ws)
$ws2.Name = '说明'
$ws2.Range('A1').Value2 = '本表为测试样本，含合并单元格、百分比、千分位、日期、公式'
$ws2.Range('A2').Value2 = '第二个工作表用来验证多表分段'

$wb.SaveAs((Join-Path $out 'sample.xlsx'), 51)   # 51 = xlOpenXMLWorkbook
$wb.Close($true)
$xl.Quit()
foreach ($o in @($ws2, $ws, $wb, $xl)) {
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($o) | Out-Null
}
[GC]::Collect()

# ---------------------------------------------------------------- docx
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
$doc = $word.Documents.Add()
$s = $word.Selection

$s.Style = $doc.Styles.Item(-2)                  # 标题 1
$s.TypeText('2026年9月全市经济运行情况')
$s.TypeParagraph()

$s.Style = $doc.Styles.Item(-1)                  # 正文
$s.TypeText('市统计局    2026年10月12日')
$s.TypeParagraph()

$s.Style = $doc.Styles.Item(-2)
$s.TypeText('一、工业')
$s.TypeParagraph()
$s.Style = $doc.Styles.Item(-1)
$s.TypeText('9月，全市规模以上工业增加值同比增长')
$s.Font.Bold = $true
$s.TypeText('7.2%')
$s.Font.Bold = $false
$s.TypeText('，比上月加快0.7个百分点。1—9月累计增长6.1%，其中9月当月增速为5.6%。')
$s.TypeParagraph()
$s.TypeText('分行业看，汽车制造业增长12.4%，电子设备制造业增长9.8%。')
$s.TypeParagraph()

# 隐藏文字：Word 里看不见，但字还在文件里。解析器必须跳过它，
# 否则「内部草稿，不要外发」这段会被当成正文喂给模型。
$s.Font.Hidden = $true
$s.TypeText('（隐藏：真实增速其实是7.25%，对外只说7.2%）')
$s.Font.Hidden = $false
$s.TypeParagraph()

$s.Style = $doc.Styles.Item(-2)
$s.TypeText('二、主要指标')
$s.TypeParagraph()
$s.Style = $doc.Styles.Item(-1)

$tbl = $doc.Tables.Add($s.Range, 6, 4)
# 横向合并：表头跨四列；纵向合并：备注列第 3、4 行并成一格。
# 这两种合并如果不补空位，读出来的表会整体错列 —— 政务报表最常见的就是这种。
$tbl.Cell(1, 1).Range.Text = '2026年9月主要指标'
$tbl.Cell(1, 1).Merge($tbl.Cell(1, 4))
$tbl.Cell(2, 1).Range.Text = '指标'
$tbl.Cell(2, 2).Range.Text = '本月（%）'
$tbl.Cell(2, 3).Range.Text = '同比（%）'
$tbl.Cell(2, 4).Range.Text = '备注'
$tbl.Cell(3, 1).Range.Text = '规上工业增加值'
$tbl.Cell(3, 2).Range.Text = '5.6'
$tbl.Cell(3, 3).Range.Text = '7.2'
$tbl.Cell(3, 4).Range.Text = '当月'
$tbl.Cell(4, 1).Range.Text = '固定资产投资'
$tbl.Cell(4, 2).Range.Text = '-3.2'
$tbl.Cell(4, 3).Range.Text = '-1.8'
$tbl.Cell(3, 4).Merge($tbl.Cell(4, 4))
$tbl.Cell(5, 1).Range.Text = '社会消费品零售总额'
$tbl.Cell(5, 2).Range.Text = '4.3'
$tbl.Cell(5, 3).Range.Text = '3.9'
$tbl.Cell(5, 4).Range.Text = '累计'
$tbl.Cell(6, 1).Range.Text = '数据来源'
$tbl.Cell(6, 2).Range.Text = '市统计局月度快报'
$tbl.Cell(6, 2).Merge($tbl.Cell(6, 4))

$s.EndKey(6) | Out-Null          # 6 = wdStory，跳出表格到文末
$s.TypeParagraph()
$s.TypeText('注：本表数据为初步核算数。')

$doc.SaveAs2([ref](Join-Path $out 'sample.docx'), [ref]16)   # 16 = docx
$doc.Close(0)
$word.Quit()
foreach ($o in @($tbl, $doc, $word)) {
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($o) | Out-Null
}
[GC]::Collect()

Get-ChildItem (Join-Path $out 'sample.*') | Select-Object Name, Length
