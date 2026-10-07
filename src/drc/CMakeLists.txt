find_package(nlohmann_json 3 REQUIRED)
# Prefer an installed package for offline builds; otherwise use the pinned 1.5.4 source.
find_package(Clipper2 1.5 CONFIG QUIET)
if(TARGET Clipper2::Clipper2)
  set(TM_CLIPPER2_TARGET Clipper2::Clipper2)
elseif(TARGET Clipper2)
  set(TM_CLIPPER2_TARGET Clipper2)
else()
  include(FetchContent)
  FetchContent_Declare(tm_clipper2
    GIT_REPOSITORY https://github.com/AngusJohnson/Clipper2.git
    GIT_TAG ef88ee97c0e759792e43a2b2d8072def6c9244e8
    SOURCE_SUBDIR CPP
    SYSTEM)
  set(CLIPPER2_UTILS OFF CACHE BOOL "" FORCE)
  set(CLIPPER2_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(CLIPPER2_TESTS OFF CACHE BOOL "" FORCE)
  set(CLIPPER2_USINGZ OFF CACHE STRING "" FORCE)
  FetchContent_MakeAvailable(tm_clipper2)
  set(TM_CLIPPER2_TARGET Clipper2)
endif()
add_library(tm_drc STATIC copper.cpp rule_engine.cpp rule_geometry.cpp connectivity.cpp drc.cpp)
add_library(tm::drc ALIAS tm_drc)
target_link_libraries(tm_drc PUBLIC tm::model tm::geom PRIVATE nlohmann_json::nlohmann_json ${TM_CLIPPER2_TARGET})
target_include_directories(tm_drc SYSTEM PRIVATE $<TARGET_PROPERTY:${TM_CLIPPER2_TARGET},INTERFACE_INCLUDE_DIRECTORIES>)
tm_set_warnings(tm_drc)
