-- Max Scene Packager: save a copy of a scene for an earlier 3ds Max version.
-- Values surrounded by double underscores are replaced in a temporary copy.

sourcePath = @"__SOURCE_PATH__"
outputPath = @"__OUTPUT_PATH__"
resultPath = @"__RESULT_PATH__"
targetVersion = __TARGET_VERSION__

fn writeResult status message versionData =
(
    resultFile = createFile resultPath
    format "status=%\n" status to:resultFile
    format "message=%\n" message to:resultFile
    if versionData != undefined do
    (
        format "file_version=%\n" versionData[1] to:resultFile
        format "saved_by_version=%\n" versionData[2] to:resultFile
    )
    close resultFile
)

try
(
    loaded = loadMaxFile sourcePath quiet:true
    if loaded then
    (
        saved = saveMaxFile outputPath saveAsVersion:targetVersion clearNeedSaveFlag:false useNewFile:false quiet:true
        if saved then
        (
            versionData = undefined
            try (versionData = getMaxFileVersionData outputPath) catch ()
            writeResult "success" "ok" versionData
        )
        else
        (
            writeResult "error" "saveMaxFile returned false" undefined
        )
    )
    else
    (
        writeResult "error" "loadMaxFile returned false" undefined
    )
)
catch
(
    writeResult "error" (getCurrentException()) undefined
)

quitMAX #noPrompt
