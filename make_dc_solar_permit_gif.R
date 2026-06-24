library(tidyverse)
library(sf)
library(gganimate)
library(gifski)
library(rstudioapi)

setwd(dirname(rstudioapi::getActiveDocumentContext()$path))

solar <- 
  readr::read_csv("dc_solar_permit_data/solar_all_years_geocoded.csv", 
                  show_col_types = F)

solar_2025 <-
  readr::read_csv("dc_solar_permit_data/solar_2025_geocoded.csv",
                  show_col_types = F) %>%
  mutate(year = 2025) %>%
  filter(`Geocodio State / Province` == "DC")

solar <-
  dplyr::bind_rows(solar, solar_2025) %>%
  distinct()                          %>%
  mutate(id = row_number())
  

# --- Clean column names for easier use ---
solar_clean <- solar |>
  rename(
    lat = `Geocodio Latitude`,
    lon = `Geocodio Longitude`
  ) |>
  filter(!is.na(lat), !is.na(lon), !is.na(year)) |>
  st_as_sf(coords = c("lon", "lat"), crs = 4326) |>
  select(year, id)

# --- Get DC boundary (optional but recommended for context) ---
# Option A: if you have tigris installed
# library(tigris)
# dc_boundary <- counties(state = "DC", cb = TRUE) |> st_transform(4326)

# --- Build cumulative frames ---
years <- sort(unique(solar_clean$year))

cumulative_solar <- map_dfr(years, function(y) {
  solar_clean |>
    filter(year <= y) |>
    mutate(frame_year = y)
})

# --- Plot ---
p <- ggplot() +
  # Uncomment if using dc_boundary:
  # geom_sf(data = dc_boundary, fill = "grey95", color = "grey60", linewidth = 0.4) +
  geom_sf(
    data = cumulative_solar,
    aes(geometry = geometry),
    color = "#F5A623",
    alpha = 0.5,
    size = 0.8
  ) +
  labs(
    title = "Cumulative Rooftop Solar Installations in DC",
    subtitle = "Year: {current_frame}",
    caption = "Source: DC solar permits data"
  ) +
  theme_void(base_size = 13) +
  theme(
    plot.title = element_text(face = "bold", hjust = 0.5),
    plot.subtitle = element_text(hjust = 0.5, size = 14),
    plot.background = element_rect(fill = "white", color = NA)
  ) +
  transition_manual(frame_year)

# --- Render GIF with a pause on the last frame ---
animate(
  p,
  nframes = length(years) + 10,   # 10 extra frames at the end
  fps = 2,
  width = 700,
  height = 700,
  renderer = gifski_renderer("dc_solar_growth.gif"),
  end_pause = 10                   # hold the last frame for 10 frames
)
