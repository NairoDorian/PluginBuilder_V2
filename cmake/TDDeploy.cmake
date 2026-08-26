# TDDeploy.cmake — POST_BUILD deploy step used by td_add_plugin() for standalone (non-PluginBuilder) builds.
#
#   cmake -DSRC=<built dll> -DDST_DIR=<__Plugins__/Name> -P TDDeploy.cmake
#
# Uses the rename-in-place trick: a DLL that TouchDesigner has loaded cannot be overwritten, but it CAN be
# renamed. So <Name>.dll is renamed to <Name>.dll.old (any previous .old is removed first), then the new
# build is copied into place. TouchDesigner picks the new file up on its next re-init of the plugin.

if(NOT SRC OR NOT DST_DIR)
    message(FATAL_ERROR "TDDeploy.cmake: SRC and DST_DIR are required")
endif()

get_filename_component(_name "${SRC}" NAME)
set(_dst "${DST_DIR}/${_name}")
set(_old "${_dst}.old")

file(MAKE_DIRECTORY "${DST_DIR}")

if(EXISTS "${_old}")
    file(REMOVE "${_old}")   # silently ignored if still locked
endif()

if(EXISTS "${_dst}")
    file(SHA256 "${_dst}" _cur)
    file(SHA256 "${SRC}" _new)
    if(_cur STREQUAL _new)
        message(STATUS "Deploy: ${_name} unchanged")
        return()
    endif()
    file(RENAME "${_dst}" "${_old}")
endif()

file(COPY "${SRC}" DESTINATION "${DST_DIR}")
message(STATUS "Deploy: ${_name} -> ${DST_DIR}")
