
# ============================================================
# DIGITEYE HEALTH WEB SCRAPER
# Articles published between 2022 and 2025
# ============================================================

# Install packages if needed
packages <- c(
  "httr", "rvest", "xml2", "dplyr",
  "stringr", "purrr", "lubridate", "readr"
)

new_packages <- packages[
  !packages %in% installed.packages()[, "Package"]
]

if (length(new_packages) > 0) {
  install.packages(new_packages)
}

library(httr)
library(rvest)
library(xml2)
library(dplyr)
library(stringr)
library(purrr)
library(lubridate)
library(readr)


# ============================================================
# 1. SETTINGS
# ============================================================

base_url <- "https://digiteye.in"
start_url <- "https://digiteye.in/category/health/"

start_year <- 2022
end_year <- 2025

output_file <- "digiteye_health_2022_2025.csv"
all_output_file <- "digiteye_health_all_scraped.csv"

max_pages <- 1000


site_handle <- handle(base_url)

request_headers <- add_headers(
  `User-Agent` = paste0(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ",
    "AppleWebKit/537.36 (KHTML, like Gecko) ",
    "Chrome/131.0.0.0 Safari/537.36"
  ),
  `Accept` = paste0(
    "text/html,application/xhtml+xml,application/xml;",
    "q=0.9,image/avif,image/webp,*/*;q=0.8"
  ),
  `Accept-Language` = "en-US,en;q=0.9",
  `Referer` = start_url
)


# ============================================================
# 2. FETCH WEBPAGE
# ============================================================

get_page <- function(url) {
  
  tryCatch({
    
    response <- GET(
      url,
      handle = site_handle,
      request_headers,
      timeout(30)
    )
    
    status <- status_code(response)
    
    if (status != 200) {
      message("HTTP ", status, " while fetching: ", url)
      return(NULL)
    }
    
    read_html(
      content(response, "text", encoding = "UTF-8")
    )
    
  }, error = function(e) {
    
    message("Error fetching ", url, ": ", conditionMessage(e))
    return(NULL)
    
  })
}


# ============================================================
# 3. EXTRACT ARTICLE LINKS
# ============================================================

extract_article_links <- function(page) {
  
  if (is.null(page)) {
    return(character(0))
  }
  
  # Main article heading links
  links <- page |>
    html_elements(
      "article h1 a[href],
       article h2 a[href],
       article h3 a[href],
       .entry-title a[href],
       .post-title a[href]"
    ) |>
    html_attr("href")
  
  links <- links[!is.na(links)]
  
  # Convert relative URLs to absolute URLs
  links <- url_absolute(links, base_url)
  
  # Remove fragments and trailing slashes
  links <- str_remove(links, "#.*$")
  links <- str_remove(links, "/+$")
  
  # Keep links 
  links <- links[
    str_detect(
      links,
      "^https?://(www\\.)?digiteye\\.in/"
    )
  ]
  
  # Exclude category, pagination, and common non-article pages
  links <- links[
    !str_detect(links, "/category/") &
      !str_detect(links, "/page/[0-9]+$") &
      !str_detect(links, "/tag/") &
      !str_detect(links, "/author/") &
      !str_detect(links, "/wp-content/")
  ]
  
  unique(links)
}


# ============================================================
# 4. FIND NEXT PAGE BUTTON
# ============================================================

get_next_page <- function(page, current_url) {
  
  if (is.null(page)) {
    return(NA_character_)
  }
  
  # WordPress pagination selectors
  selectors <- c(
    "a.next.page-numbers",
    "a.next",
    "a[rel='next']",
    ".nav-links a.next",
    ".pagination a.next",
    ".posts-navigation a.next",
    ".pagination a"
  )
  
  for (selector in selectors) {
    
    nodes <- page |> html_elements(selector)
    
    if (length(nodes) == 0) {
      next
    }
    
    for (node in nodes) {
      
      href <- html_attr(node, "href")
      label <- html_text2(node) |> str_squish()
      
      if (!is.na(href) && nzchar(href)) {
        
        # For generic pagination links, require a Next label
        if (
          selector == ".pagination a" &&
          !str_detect(str_to_lower(label), "next|»|›")
        ) {
          next
        }
        
        next_url <- url_absolute(href, current_url)
        
        if (
          str_detect(
            next_url,
            "^https?://(www\\.)?digiteye\\.in/"
          )
        ) {
          return(next_url)
        }
      }
    }
  }
  
  # Fallback
  links <- page |> html_elements("a")
  
  labels <- links |>
    html_text2() |>
    str_squish() |>
    str_to_lower()
  
  next_indices <- which(
    str_detect(labels, "^next(\\s+page)?\\s*(»|›)?$")
  )
  
  if (length(next_indices) > 0) {
    
    href <- links[[next_indices[1]]] |> html_attr("href")
    
    if (!is.na(href) && nzchar(href)) {
      return(url_absolute(href, current_url))
    }
  }
  
  NA_character_
}


# ============================================================
# 5. SCRAPE CATEGORY PAGES
# ============================================================

all_article_urls <- character(0)
visited_pages <- character(0)

current_url <- start_url
page_number <- 1

while (
  !is.na(current_url) &&
  nzchar(current_url) &&
  page_number <= max_pages
) {
  
  if (current_url %in% visited_pages) {
    message("Page already visited. Stopping.")
    break
  }
  
  message(
    "\nScraping category page ",
    page_number, ": ", current_url
  )
  
  visited_pages <- c(visited_pages, current_url)
  
  category_page <- get_page(current_url)
  
  if (is.null(category_page)) {
    message("Could not retrieve category page. Stopping.")
    break
  }
  
  article_links <- extract_article_links(category_page)
  
  new_links <- setdiff(article_links, all_article_urls)
  
  all_article_urls <- unique(
    c(all_article_urls, new_links)
  )
  
  message("New article URLs: ", length(new_links))
  message("Total unique URLs: ", length(all_article_urls))
  
  next_url <- get_next_page(category_page, current_url)
  
  if (is.na(next_url) || !nzchar(next_url)) {
    message("Next Page link not found. Stopping.")
    break
  }
  
  current_url <- next_url
  page_number <- page_number + 1
  
  Sys.sleep(1)
}

message("\nCategory scraping finished.")
message("Pages visited: ", length(visited_pages))
message("Unique article URLs: ", length(all_article_urls))


# ============================================================
# 6. HELPER: EXTRACT FIRST MATCHING TEXT
# ============================================================

first_text <- function(page, selectors) {
  
  for (selector in selectors) {
    
    node <- page |> html_element(selector)
    
    if (inherits(node, "xml_missing")) {
      next
    }
    
    value <- html_text2(node) |> str_squish()
    
    if (!is.na(value) && nzchar(value)) {
      return(value)
    }
  }
  
  NA_character_
}


# ============================================================
# 7. HELPER: EXTRACT PUBLICATION DATE
# ============================================================

extract_publication_date <- function(page) {
  
  # ----------------------------------------------------------
  # A. Extract visible text from article header
  # ----------------------------------------------------------
  
  header_text <- page |>
    html_elements(
      "article h1,
       article header,
       .entry-header,
       .post-header"
    ) |>
    html_text2() |>
    str_squish() |>
    paste(collapse = " ")
  
  
  
  date_pattern <- paste0(
    "\\b[A-Z][a-z]+\\s+\\d{1,2},\\s+\\d{4}\\b"
  )
  
  date_match <- str_extract(header_text, date_pattern)
  
  # ----------------------------------------------------------
  # B. If not found, article date-related elements
  # ----------------------------------------------------------
  
  if (is.na(date_match)) {
    
    date_text <- page |>
      html_elements(
        ".posted-on,
         .entry-date,
         .post-date,
         .date,
         time"
      ) |>
      html_text2() |>
      str_squish()
    
    date_text <- date_text[
      !is.na(date_text) & nzchar(date_text)
    ]
    
    if (length(date_text) > 0) {
      
      date_match <- str_extract(
        paste(date_text, collapse = " "),
        date_pattern
      )
    }
  }
  
  # ----------------------------------------------------------
  # C. If not found, publication metadata
  # ----------------------------------------------------------
  
  if (is.na(date_match)) {
    
    metadata_date <- page |>
      html_elements(
        "meta[property='article:published_time'],
         meta[name='pubdate'],
         meta[name='date'],
         meta[itemprop='datePublished']"
      ) |>
      html_attr("content")
    
    metadata_date <- metadata_date[
      !is.na(metadata_date) & nzchar(metadata_date)
    ]
    
    if (length(metadata_date) > 0) {
      
      parsed_metadata <- suppressWarnings(
        parse_date_time(
          metadata_date,
          orders = c(
            "ymd HMS", "ymd HM", "ymd",
            "dmy HMS", "dmy HM", "dmy",
            "mdy HMS", "mdy HM", "mdy"
          ),
          quiet = TRUE
        )
      )
      
      valid_metadata <- parsed_metadata[
        !is.na(parsed_metadata)
      ]
      
      if (length(valid_metadata) > 0) {
        return(as.Date(valid_metadata[1]))
      }
    }
  }
  
  # ----------------------------------------------------------
  # D. Parse the visible date
  # ----------------------------------------------------------
  
  if (!is.na(date_match)) {
    
    parsed_date <- suppressWarnings(
      mdy(date_match, quiet = TRUE)
    )
    
    if (length(parsed_date) > 0 && !is.na(parsed_date[1])) {
      return(as.Date(parsed_date[1]))
    }
  }
  
  return(as.Date(NA))
}


# ============================================================
# 8. EXTRACT ARTICLE INFORMATION
# ============================================================

extract_article <- function(url) {
  
  message("Extracting article: ", url)
  
  page <- get_page(url)
  
  if (is.null(page)) {
    
    return(tibble(
      title = NA_character_,
      date = as.Date(NA),
      year = NA_integer_,
      author = NA_character_,
      url = url,
      article_text = NA_character_
    ))
  }
  
  # ----------------------------------------------------------
  # TITLE
  # ----------------------------------------------------------
  
  title <- first_text(
    page,
    c(
      "h1.entry-title",
      "h1.post-title",
      "article h1",
      "h1"
    )
  )
  
  # ----------------------------------------------------------
  # DATE AND YEAR
  # ----------------------------------------------------------
  
  article_date <- extract_publication_date(page)
  
  article_year <- if (is.na(article_date)) {
    NA_integer_
  } else {
    lubridate::year(article_date)
  }
  
  # ----------------------------------------------------------
  # AUTHOR
  # ----------------------------------------------------------
  
  author <- first_text(
    page,
    c(
      ".author a",
      ".author",
      "[rel='author']",
      ".byline"
    )
  )
  
  # ----------------------------------------------------------
  # ARTICLE TEXT
  # ----------------------------------------------------------
  
  article_text <- NA_character_
  
  content_selectors <- c(
    ".entry-content",
    ".post-content",
    ".td-post-content",
    ".article-content",
    "article"
  )
  
  for (selector in content_selectors) {
    
    content_node <- page |> html_element(selector)
    
    if (inherits(content_node, "xml_missing")) {
      next
    }
    
    # Remove unwanted elements
    unwanted <- content_node |>
      html_elements(
        "script,
         style,
         nav,
         footer,
         aside,
         form,
         iframe,
         .sharedaddy,
         .related-posts,
         .jp-relatedposts,
         .advertisement"
      )
    
    if (length(unwanted) > 0) {
      xml_remove(unwanted)
    }
    
    paragraphs <- content_node |>
      html_elements("p") |>
      html_text2() |>
      str_squish()
    
    paragraphs <- paragraphs[
      !is.na(paragraphs) & nzchar(paragraphs)
    ]
    
    if (length(paragraphs) > 0) {
      
      article_text <- paste(
        paragraphs,
        collapse = "\n"
      )
      
      break
    }
  }
  
  # ----------------------------------------------------------
  # RETURN ARTICLE DATA
  # ----------------------------------------------------------
  
  tibble(
    title = title,
    date = article_date,
    year = article_year,
    author = author,
    url = url,
    article_text = article_text
  )
}


# ============================================================
# 9. SCRAPE ALL COLLECTED ARTICLES
# ============================================================

results <- vector("list", length(all_article_urls))

for (i in seq_along(all_article_urls)) {
  
  message(
    "\nArticle ", i,
    " of ", length(all_article_urls)
  )
  
  results[[i]] <- extract_article(all_article_urls[i])
  
  Sys.sleep(1)
}

df <- bind_rows(results) |>
  distinct(url, .keep_all = TRUE)


# ============================================================
# 10. FILTER ARTICLES: 2022–2025
# ============================================================

df_filtered <- df |>
  filter(
    !is.na(year),
    year >= start_year,
    year <= end_year
  ) |>
  distinct(url, .keep_all = TRUE) |>
  arrange(date)


# ============================================================
# 11. SCRAPING SUMMARY
# ============================================================

cat("\n========================================\n")
cat("DIGITEYE HEALTH SCRAPING SUMMARY\n")
cat("========================================\n")

cat("Category pages visited:", length(visited_pages), "\n")
cat("Unique article URLs:", length(all_article_urls), "\n")
cat("Articles extracted:", nrow(df), "\n")
cat("Articles dated 2022–2025:", nrow(df_filtered), "\n")

cat(
  "Articles with missing dates:",
  sum(is.na(df$date)),
  "\n"
)

cat(
  "Articles with missing text:",
  sum(is.na(df$article_text) | df$article_text == ""),
  "\n"
)

cat("\nArticles by year:\n")

print(
  df_filtered |>
    count(year, name = "number_of_articles") |>
    arrange(year)
)


# ============================================================
# 12. EXPORT DATASETS
# ============================================================

# Dataset restricted to 2022–2025
write_csv(
  df_filtered,
  output_file,
  na = ""
)

# All scraped articles, including those outside the period
write_csv(
  df,
  all_output_file,
  na = ""
)

cat("\nSaved filtered dataset:", output_file, "\n")
cat("Saved complete dataset:", all_output_file, "\n")


# ============================================================
# 13. OPTIONAL: SAVE ARTICLES WITH MISSING DATES
# ============================================================

missing_dates <- df |>
  filter(is.na(date))

write_csv(
  missing_dates,
  "digiteye_health_missing_dates.csv",
  na = ""
)

cat(
  "Saved articles with missing dates:",
  "digiteye_health_missing_dates.csv\n"
)

