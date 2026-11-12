param(
    [Parameter(Mandatory=$true)][Int64]$WindowHandle,
    [int]$MaxNodes = 2200,
    [int]$MaxMilliseconds = 6000,
    [int]$MaxChars = 240000,
    [string]$EditorRegex = '^(?:(?:Message ChatGPT|Ask anything|向 ChatGPT 发送消息|询问任何问题)$|(?:发消息或创建任务|Message or run a task|描述你想要构建的内容|Describe what you want to build|描述你的任务以生成计划|describe your task to generate plan|Cmd/Ctrl\+Enter 插话发送全部排队消息|Cmd/Ctrl\+Enter steers all queued messages)(?:$|[\s,，].*))'
)
# Read-only UIAutomation: no Invoke/SetValue/focus/input and no application settings.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
function Get-EditorCandidate([string]$TypeName, [string]$Name, [bool]$InDocument,
                             [bool]$HasValue, [bool]$ValueReadOnly, [string]$Pattern) {
    # This pure classifier intentionally excludes static labels and readonly output.
    $capable = $TypeName -in @('ControlType.Edit', 'ControlType.Document', 'ControlType.Custom', 'ControlType.Pane')
    if (-not $capable -or ($HasValue -and $ValueReadOnly)) {
        return @{ Named=$false; Fallback=$false; Candidate=$false; Required=$false }
    }
    $named = -not [string]::IsNullOrWhiteSpace($Name) -and $Name -match $Pattern
    $fallback = $InDocument -and ($TypeName -eq 'ControlType.Edit' -or ($HasValue -and -not $ValueReadOnly))
    $required = ($named -and $TypeName -in @('ControlType.Edit', 'ControlType.Document')) -or ($fallback -and $TypeName -eq 'ControlType.Edit')
    return @{ Named=$named; Fallback=$fallback; Candidate=($named -or $fallback); Required=$required }
}
function Test-EditorPlaceholder([string]$Text, [string]$Name, [string]$Pattern) {
    $trimmed = $Text.Trim()
    return -not [string]::IsNullOrWhiteSpace($trimmed) -and (
        $trimmed -eq $Name.Trim() -or $trimmed -match $Pattern
    )
}
function Select-EditorState([string[]]$NamedDrafts, [bool]$NamedUnreadable,
                            [string[]]$FallbackDrafts, [bool]$FallbackUnreadable,
                            [string[]]$NamedAmbiguous=@(), [string[]]$FallbackAmbiguous=@()) {
    # Readable named composers take precedence over code/search/tool fallback edits.
    if ($NamedDrafts.Count -gt 0) { return @{ Text=($NamedDrafts -join "`n"); Present=$true; Readable=$true; Ambiguous=$false; Source='named' } }
    if ($NamedAmbiguous.Count -gt 0) { return @{ Text=($NamedAmbiguous -join "`n"); Present=$true; Readable=$false; Ambiguous=$true; Source='named_text_ambiguous' } }
    if ($NamedUnreadable) { return @{ Text=''; Present=$true; Readable=$false; Source='named_unreadable' } }
    if ($FallbackDrafts.Count -gt 0) { return @{ Text=($FallbackDrafts -join "`n"); Present=$true; Readable=$true; Ambiguous=$false; Source='fallback' } }
    if ($FallbackAmbiguous.Count -gt 0) { return @{ Text=($FallbackAmbiguous -join "`n"); Present=$true; Readable=$false; Ambiguous=$true; Source='fallback_text_ambiguous' } }
    if ($FallbackUnreadable) { return @{ Text=''; Present=$true; Readable=$false; Source='fallback_unreadable' } }
    return @{ Text=''; Present=$false; Readable=$false; Source='not_found' }
}
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
    if ($null -eq $root) { throw 'The selected window is unavailable.' }
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $queue = New-Object 'System.Collections.Generic.Queue[object]'
    $queue.Enqueue(@{ Element=$root; InDocument=$false })
    $fragments = New-Object 'System.Collections.Generic.List[string]'
    $seenText = New-Object 'System.Collections.Generic.HashSet[string]'
    $buttons = New-Object 'System.Collections.Generic.List[string]'
    $enabledButtons = New-Object 'System.Collections.Generic.List[string]'
    $namedDrafts = New-Object 'System.Collections.Generic.List[string]'
    $fallbackDrafts = New-Object 'System.Collections.Generic.List[string]'
    $namedAmbiguous = New-Object 'System.Collections.Generic.List[string]'
    $fallbackAmbiguous = New-Object 'System.Collections.Generic.List[string]'
    $candidateDiagnostics = New-Object 'System.Collections.Generic.List[object]'
    $namedUnreadable = $false
    $fallbackUnreadable = $false
    $seenNodes = New-Object 'System.Collections.Generic.HashSet[string]'
    $script:truncated = $false
    $script:chars = 0
    $nodes = 0
    $watch = [Diagnostics.Stopwatch]::StartNew()
    function Add-VisibleText([string]$value) {
        if ([string]::IsNullOrWhiteSpace($value) -or -not $seenText.Add($value)) { return }
        $remaining = $MaxChars - $script:chars
        if ($remaining -le 0) { $script:truncated = $true; return }
        if ($value.Length -gt $remaining) {
            $value = $value.Substring(0, $remaining)
            $script:truncated = $true
        }
        $fragments.Add($value)
        $script:chars += $value.Length + 1
    }
    while ($queue.Count -gt 0 -and -not $script:truncated) {
        if ($nodes -ge $MaxNodes -or $watch.ElapsedMilliseconds -ge $MaxMilliseconds) {
            $script:truncated = $true
            break
        }
        $entry = $queue.Dequeue()
        $node = $entry.Element
        $nodes++
        try {
            $key = ($node.GetRuntimeId() -join '.')
            if (-not $seenNodes.Add($key)) { continue }
            $current = $node.Current
            # Hidden branches contribute neither historical text nor hidden controls.
            if ($current.IsOffscreen) { continue }
            $type = $current.ControlType
            $inDocument = $entry.InDocument -or ($type -eq [System.Windows.Automation.ControlType]::Document)
            $name = [string]$current.Name
            $valuePattern = $null
            $hasValue = $node.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)
            $valueReadOnly = $hasValue -and $valuePattern.Current.IsReadOnly
            $candidate = Get-EditorCandidate $type.ProgrammaticName $name $inDocument $hasValue $valueReadOnly $EditorRegex
            if ($type -eq [System.Windows.Automation.ControlType]::Button -and -not [string]::IsNullOrWhiteSpace($name)) {
                $buttons.Add($name)
                if ($current.IsEnabled) { $enabledButtons.Add($name) }
            }
            if ($candidate.Candidate) {
                $draftRead = $false
                $ambiguous = $false
                $draft = ''
                $diagnostic = @{
                    name=$name.Substring(0, [Math]::Min(160, $name.Length)); type=$type.ProgrammaticName
                    named=[bool]$candidate.Named; fallback=[bool]$candidate.Fallback; has_value=[bool]$hasValue
                    value_readonly=[bool]$valueReadOnly; value_preview=''; text_preview=''; read_source=''; result='unreadable'
                    automation_id=''; class_name=''; bounding_rect=@(); parent_name=''; read_pattern=''
                    value_length=$null; text_length=$null
                }
                try {
                    $diagnostic.automation_id = ([string]$current.AutomationId).Substring(0, [Math]::Min(160, ([string]$current.AutomationId).Length))
                    $diagnostic.class_name = ([string]$current.ClassName).Substring(0, [Math]::Min(160, ([string]$current.ClassName).Length))
                    $bounds = $current.BoundingRectangle
                    $diagnostic.bounding_rect = @($bounds.X, $bounds.Y, $bounds.Width, $bounds.Height)
                    $parent = $walker.GetParent($node)
                    if ($null -ne $parent) {
                        $parentName = [string]$parent.Current.Name
                        $diagnostic.parent_name = $parentName.Substring(0, [Math]::Min(160, $parentName.Length))
                    }
                } catch { }
                $editorPattern = $null
                $hasText = $node.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$editorPattern)
                if ($hasValue) {
                    # A writable ValuePattern is the actual editor value. Prefer
                    # it over a TextPattern range which may contain a label.
                    $draft = [string]$valuePattern.Current.Value
                    $draftRead = $true
                    $diagnostic.read_source = 'value'
                    $diagnostic.read_pattern = 'ValuePattern'
                    $diagnostic.value_length = $draft.Length
                    $diagnostic.value_preview = $draft.Substring(0, [Math]::Min(160, $draft.Length))
                    if ($hasText) {
                        try {
                            $preview = [string]$editorPattern.DocumentRange.GetText(160)
                            $diagnostic.text_preview = $preview
                        } catch { $diagnostic.text_preview = '[unreadable]' }
                    }
                } elseif ($hasText) {
                    $textReadOnly = $editorPattern.DocumentRange.GetAttributeValue([System.Windows.Automation.TextPattern]::IsReadOnlyAttribute)
                    if ($textReadOnly -is [bool] -and $textReadOnly) {
                        $candidate.Candidate = $false
                        $diagnostic.result = 'readonly_text'
                    } else {
                        $draft = [string]$editorPattern.DocumentRange.GetText($MaxChars + 1)
                        $draftRead = $true
                        $diagnostic.read_source = 'text'
                        $diagnostic.read_pattern = 'TextPattern'
                        $diagnostic.text_length = $draft.Length
                        $diagnostic.text_preview = $draft.Substring(0, [Math]::Min(160, $draft.Length))
                        $ambiguous = Test-EditorPlaceholder $draft $name $EditorRegex
                    }
                }
                if ($draftRead) {
                    if ($draft.Length -gt $MaxChars) { $script:truncated = $true }
                    if ($ambiguous) {
                        $diagnostic.result = 'ambiguous_placeholder'
                        if ($candidate.Named) { $namedAmbiguous.Add($draft) } else { $fallbackAmbiguous.Add($draft) }
                    } else {
                        $diagnostic.result = 'readable'
                        if ($candidate.Named) { $namedDrafts.Add($draft) } else { $fallbackDrafts.Add($draft) }
                    }
                    if ($candidateDiagnostics.Count -lt 8) { $candidateDiagnostics.Add($diagnostic) }
                    # Ambiguous inherited labels keep their text for diagnostics
                    # and allow a child exposing a real value to be discovered.
                    if (-not $ambiguous) { continue }
                } else {
                    if ($candidateDiagnostics.Count -lt 8) { $candidateDiagnostics.Add($diagnostic) }
                }
                if (-not $draftRead -and $candidate.Candidate -and $candidate.Required) {
                    if ($candidate.Named) { $namedUnreadable = $true } else { $fallbackUnreadable = $true }
                }
                # A Custom/Pane may inherit a composer's accessible name without
                # exposing an editor pattern. Keep walking to its actual Edit child.
            }
            Add-VisibleText $name
            $textPattern = $null
            if ($node.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$textPattern)) {
                foreach ($range in $textPattern.GetVisibleRanges()) {
                    $remaining = [Math]::Max(1, $MaxChars - $script:chars + 1)
                    Add-VisibleText ([string]$range.GetText($remaining))
                    if ($script:truncated) { break }
                }
            }
            # The composer/newest messages are usually the final children. Start
            # there, and never enqueue entire offscreen history branches.
            $child = $walker.GetLastChild($node)
            while ($null -ne $child) {
                if ($queue.Count + $nodes -ge $MaxNodes -or $watch.ElapsedMilliseconds -ge $MaxMilliseconds) {
                    $script:truncated = $true
                    break
                }
                if (-not $child.Current.IsOffscreen) {
                    $queue.Enqueue(@{ Element=$child; InDocument=$inDocument })
                }
                $child = $walker.GetPreviousSibling($child)
            }
        } catch {
            # A stale/inaccessible branch could contain a stop button or message.
            # Do not silently turn a partial tree into a successful idle observation.
            $script:truncated = $true
        }
    }
    $watch.Stop()
    $editor = Select-EditorState -NamedDrafts $namedDrafts.ToArray() -NamedUnreadable $namedUnreadable -FallbackDrafts $fallbackDrafts.ToArray() -FallbackUnreadable $fallbackUnreadable -NamedAmbiguous $namedAmbiguous.ToArray() -FallbackAmbiguous $fallbackAmbiguous.ToArray()
    $result = @{
        text=($fragments -join "`n"); buttons=@($buttons.ToArray()); enabled_buttons=@($enabledButtons.ToArray())
        editable_text=$editor.Text; editable_present=$editor.Present; editable_readable=$editor.Readable; editor_source=$editor.Source
        editor_ambiguous=[bool]$editor.Ambiguous; editor_candidates=@($candidateDiagnostics.ToArray())
        truncated=[bool]$script:truncated; nodes=$nodes; elapsed_ms=$watch.ElapsedMilliseconds
    }
    ConvertTo-Json -InputObject $result -Depth 5 -Compress
} catch {
    ConvertTo-Json -InputObject @{ text=''; buttons=@(); enabled_buttons=@(); editable_text=''; editable_present=$false; truncated=$true; error=$_.Exception.Message } -Depth 3 -Compress
}
