# ============================================================
# DFRAC - GENERATIVE AI
# Scrape articles published in 2022-2025
# Output: one CSV per year
# Columns: author, date, title, content
# ============================================================

library(rvest)
library(xml2)
library(httr2)
library(dplyr)
library(stringr)
library(purrr)
library(readr)
library(lubridate)

# ------------------------------------------------------------
# 1. SETTINGS
# ------------------------------------------------------------

base_url <- "https://dfrac.org/en/topic/generative-ai/"

output_dir <- "DFRAC_Generative_AI"

if (!dir.exists(output_dir)) {
  dir.create(output_dir)
}

years <- 2022:2025


# ------------------------------------------------------------
# 2. GET PAGE
# ------------------------------------------------------------

get_page <- function(url) {
  
  Sys.sleep(1)
  
  tryCatch({
    
    response <- request(url) |>
      req_user_agent(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      ) |>
      req_timeout(30) |>
      req_perform()
    
    read_html(resp_body_string(response))
    
  }, error = function(e) {
    
    message("ERROR: ", url)
    message(e$message)
    
    NULL
  })
}


# ------------------------------------------------------------
# 3. GET ARTICLE LINKS FROM ONE ARCHIVE PAGE
# ------------------------------------------------------------

get_dfrac_links <- function(page_num = 1) {
  
  if (page_num == 1) {
    url <- base_url
  } else {
    url <- paste0(
      "https://dfrac.org/en/topic/generative-ai/page/",
      page_num,
      "/"
    )
  }
  
  message("Checking page: ", page_num)
  
  page <- get_page(url)
  
  if (is.null(page)) {
    return(character(0))
  }
  
  links <- page |>
    html_elements("h2 a") |>
    html_attr("href")
  
  links <- links[!is.na(links)]
  
  links <- url_absolute(
    links,
    "https://dfrac.org"
  )
  
  # Keep only article URLs belonging to DFRAC
  links <- links[
    str_detect(
      links,
      "^https://dfrac\\.org/en/[0-9]{4}/"
    )
  ]
  
  unique(links)
}


# ------------------------------------------------------------
# 4. SCRAPE ALL PAGINATION
# ------------------------------------------------------------

all_links <- character(0)

max_pages <- 20

for (p in 1:max_pages) {
  
  links <- get_dfrac_links(p)
  
  message(
    "Found ",
    length(links),
    " article links on page ",
    p
  )
  
  if (length(links) == 0) {
    message("No more articles found. Stopping.")
    break
  }
  
  old_n <- length(all_links)
  
  all_links <- unique(
    c(all_links, links)
  )
  
  # Stop if page adds no new URLs
  if (length(all_links) == old_n) {
    
    message(
      "No new URLs found on page ",
      p,
      ". Stopping."
    )
    
    break
  }
}


# ------------------------------------------------------------
# 5. CHECK NUMBER OF UNIQUE ARTICLES
# ------------------------------------------------------------

message(
  "\nTotal unique article URLs: ",
  length(all_links)
)


# ------------------------------------------------------------
# 6. SCRAPE INDIVIDUAL ARTICLE
# ------------------------------------------------------------

scrape_article <- function(url) {
  
  message("Scraping: ", url)
  
  page <- get_page(url)
  
  if (is.null(page)) {
    
    return(
      tibble(
        url = url,
        author = NA_character_,
        date = NA_character_,
        title = NA_character_,
        content = NA_character_
      )
    )
  }
  
  # TITLE
  title <- page |>
    html_element("h1") |>
    html_text2()
  
  if (length(title) == 0) {
    title <- NA_character_
  }
  
  # AUTHOR
  author <- page |>
    html_elements(
      ".author-name, .author, .byline"
    ) |>
    html_text2()
  
  if (length(author) == 0) {
    author <- NA_character_
  } else {
    author <- author[1]
  }
  
  # DATE
  date_text <- page |>
    html_elements(
      "time, .entry-date, .posted-on"
    ) |>
    html_text2()
  
  if (length(date_text) == 0) {
    date_text <- NA_character_
  } else {
    date_text <- date_text[1]
  }
  
  # CONTENT
  content_node <- page |>
    html_element(
      ".entry-content"
    )
  
  if (is.na(content_node)) {
    
    content_node <- page |>
      html_element(
        ".post-content"
      )
  }
  
  if (is.na(content_node)) {
    
    content_node <- page |>
      html_element(
        "article"
      )
  }
  
  if (is.na(content_node)) {
    
    content <- NA_character_
    
  } else {
    
    # Remove irrelevant elements
    html_elements(
      content_node,
      "script, style, figure, .sharedaddy, .jp-relatedposts"
    ) |>
      xml_remove()
    
    content <- html_text2(
      content_node
    )
    
    content <- str_squish(content)
  }
  
  tibble(
    url = url,
    author = author,
    date = date_text,
    title = title,
    content = content
  )
}


# ------------------------------------------------------------
# 7. SCRAPE ARTICLES
# ------------------------------------------------------------

articles <- map_dfr(
  all_links,
  scrape_article
)


# ------------------------------------------------------------
# 8. CLEAN DATE
# ------------------------------------------------------------

articles <- articles |>
  mutate(
    # Extract the first Month Day, Year date
    date_clean = str_extract(
      date,
      "[A-Z][a-z]+\\s+\\d{1,2},\\s+\\d{4}"
    ),
    
    date_parsed = mdy(date_clean),
    
    year = year(date_parsed)
  )


# Check dates
print(
  articles |>
    select(date, date_clean, date_parsed, year) |>
    head(20)
)


# ------------------------------------------------------------
# 9. KEEP ONLY 2022-2025
# ------------------------------------------------------------

articles_2022_2025 <- articles |>
  filter(
    year %in% years
  ) |>
  arrange(date_parsed)


# Check number of articles
message(
  "\nArticles from 2022-2025: ",
  nrow(articles_2022_2025)
)


# ------------------------------------------------------------
# 10. EXPORT ONE CSV PER YEAR
# ------------------------------------------------------------

for (yr in years) {
  
  yearly_data <- articles_2022_2025 |>
    filter(year == yr) |>
    select(
      author,
      date = date_clean,
      title,
      content
    )
  
  output_file <- file.path(
    output_dir,
    paste0(
      "DFRAC_Generative_AI_",
      yr,
      ".csv"
    )
  )
  
  write_csv(
    yearly_data,
    output_file
  )
  
  message(
    "Saved ",
    yr,
    ": ",
    nrow(yearly_data),
    " articles"
  )
}


# ------------------------------------------------------------
# 11. SUMMARY
# ------------------------------------------------------------
summary_table <- articles_2022_2025 |>
  count(year) |>
  arrange(year)

print(summary_table)

message("\nDONE.")

write_csv(articles, "C:\\Users\\gowri\\OneDrive\\Desktop\\summer_internship\\dfrac_articles.csv")
