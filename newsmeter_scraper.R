# ============================================================
# NEWSMETER HEALTH FACT-CHECK SCRAPER
# COMPLETE ARCHIVE: PAGES 1–31
# TARGET YEARS: 2022–2025
# ============================================================


# ============================================================
# 0. PACKAGES
# ============================================================

library(httr2)
library(rvest)
library(dplyr)
library 
library(stringr)
library(readr)
library(jsonlite)


# ============================================================
# 1. SETTINGS
# ============================================================

base_url <- "https://newsmeter.in/fact-check-health"

user_agent <- paste(
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
  "AppleWebKit/537.36 (KHTML, like Gecko)",
  "Chrome/131.0.0.0 Safari/537.36"
)


# ============================================================
# 2. FUNCTION: DOWNLOAD ARCHIVE PAGE
# ============================================================

get_archive_page <- function(page_number) {
  
  if (page_number == 1) {
    
    page_url <- base_url
    
  } else {
    
    page_url <- paste0(
      base_url,
      "/",
      page_number
    )
  }
  
  
  response <- request(page_url) |>
    req_user_agent(user_agent) |>
    req_timeout(30) |>
    req_perform()
  
  
  html <- resp_body_html(response)
  
  
  list(
    page = page_number,
    page_url = page_url,
    html = html
  )
}


# ============================================================
# 3. DOWNLOAD ALL 31 ARCHIVE PAGES
# ============================================================

page_results <- vector(
  "list",
  31
)


for (p in 1:31) {
  
  cat("\n")
  cat("============================================================\n")
  cat("DOWNLOADING PAGE", p, "OF 31\n")
  cat("============================================================\n")
  
  
  page_results[[p]] <- tryCatch({
    
    result <- get_archive_page(p)
    
    cat(
      "Successfully downloaded page",
      p,
      "\n"
    )
    
    result
    
  }, error = function(e) {
    
    cat(
      "ERROR ON PAGE",
      p,
      ":",
      conditionMessage(e),
      "\n"
    )
    
    NULL
  })
  
  
  Sys.sleep(1)
}


# ============================================================
# 4. EXTRACT ARTICLE URLS FROM EACH PAGE
# ============================================================

page_article_data <- vector(
  "list",
  31
)


for (p in 1:31) {
  
  cat(
    "\nExtracting article links from page",
    p,
    "...\n"
  )
  
  
  result <- page_results[[p]]
  
  
  if (is.null(result)) {
    
    page_article_data[[p]] <- tibble(
      page = p,
      article_url = character(),
      link_text = character()
    )
    
    next
  }
  
  
  html <- result$html
  
  
  # ----------------------------------------------------------
  # Get all links
  # ----------------------------------------------------------
  
  links <- html_elements(
    html,
    "a"
  )
  
  
  href <- html_attr(
    links,
    "href"
  )
  
  
  link_text <- html_text2(
    links
  )
  
  
  # ----------------------------------------------------------
  # Remove NA / blank links
  # ----------------------------------------------------------
  
  keep <- !is.na(href) &
    str_trim(href) != ""
  
  
  href <- href[keep]
  link_text <- link_text[keep]
  
  
  # ----------------------------------------------------------
  # Convert relative URLs to absolute
  # ----------------------------------------------------------
  
  href <- url_absolute(
    href,
    "https://newsmeter.in/"
  )
  
  
  # ----------------------------------------------------------
  # KEEP FACT-CHECK ARTICLE URLs
  # ----------------------------------------------------------
  
  is_factcheck <- str_detect(
    href,
    "^https://newsmeter\\.in/fact-check(-health)?/.+"
  )
  
  
  article_href <- href[is_factcheck]
  article_text <- link_text[is_factcheck]
  
  
  # ----------------------------------------------------------
  # Remove pagination URLs
  # ----------------------------------------------------------
  
  is_pagination <- str_detect(
    article_href,
    "^https://newsmeter\\.in/fact-check-health/[0-9]+/?$"
  )
  
  
  article_href <- article_href[
    !is_pagination
  ]
  
  article_text <- article_text[
    !is_pagination
  ]
  
  
  # ----------------------------------------------------------
  # Remove query strings / fragments
  # ----------------------------------------------------------
  
  article_href <- str_remove(
    article_href,
    "[?#].*$"
  )
  
  
  # ----------------------------------------------------------
  # Remove blank / NA URLs
  # ----------------------------------------------------------
  
  keep2 <- !is.na(article_href) &
    str_trim(article_href) != ""
  
  
  article_href <- article_href[keep2]
  article_text <- article_text[keep2]
  
  
  # ----------------------------------------------------------
  # Store
  # ----------------------------------------------------------
  
  page_data <- tibble(
    page = p,
    article_url = article_href,
    link_text = article_text
  ) |>
    distinct(
      article_url,
      .keep_all = TRUE
    )
  
  
  page_article_data[[p]] <- page_data
  
  
  cat(
    "Articles found:",
    nrow(page_data),
    "\n"
  )
}


# ============================================================
# 5. COMBINE ALL PAGES
# ============================================================

all_page_articles <- bind_rows(
  page_article_data
)


# ============================================================
# 6. PAGE-BY-PAGE COUNTS
# ============================================================

page_counts <- tibble(
  page = 1:31
) |>
  left_join(
    all_page_articles |>
      count(
        page,
        name = "article_count"
      ),
    by = "page"
  ) |>
  mutate(
    article_count = coalesce(
      article_count,
      0L
    )
  ) |>
  arrange(page)


# ============================================================
# 7. PRINT PAGE COUNTS
# ============================================================

cat("\n")
cat("============================================================\n")
cat("ARTICLE COUNT ON EACH PAGE\n")
cat("============================================================\n")

print(page_counts)


# ============================================================
# 8. TOTAL ARTICLE LINKS
# ============================================================

cat("\n")
cat("============================================================\n")
cat("TOTAL ARTICLE-LINK OCCURRENCES\n")
cat("============================================================\n")

cat(
  sum(page_counts$article_count),
  "\n"
)


# ============================================================
# 9. UNIQUE ARTICLE URLs
# ============================================================

article_urls <- all_page_articles |>
  distinct(
    article_url,
    .keep_all = TRUE
  ) |>
  filter(
    !is.na(article_url),
    str_trim(article_url) != ""
  )


cat("\n")
cat("============================================================\n")
cat("TOTAL UNIQUE ARTICLE URLS\n")
cat("============================================================\n")

cat(
  nrow(article_urls),
  "\n"
)


# ============================================================
# 10. PAGE 31 CHECK
# ============================================================

cat("\n")
cat("============================================================\n")
cat("PAGE 31 CHECK\n")
cat("============================================================\n")

print(
  all_page_articles |>
    filter(page == 31)
)


# ============================================================
# 11. SAVE URL DATA
# ============================================================

write_csv(
  page_counts,
  "newsmeter_health_page_counts_1_31.csv"
)


write_csv(
  article_urls,
  "newsmeter_health_article_urls_pages_1_31.csv"
)


# ============================================================
# 12. FUNCTION TO EXTRACT JSON-LD ARTICLE DATA
# ============================================================

extract_jsonld_article <- function(html) {
  
  scripts <- html |>
    html_elements(
      "script[type='application/ld+json']"
    ) |>
    html_text2()
  
  
  if (length(scripts) == 0) {
    
    return(
      list(
        headline = NA_character_,
        datePublished = NA_character_,
        articleBody = NA_character_
      )
    )
  }
  
  
  # ----------------------------------------------------------
  # Recursive function to search JSON objects
  # ----------------------------------------------------------
  
  find_article_data <- function(x) {
    
    if (is.list(x)) {
      
      # Direct Article object
      if (
        !is.null(x$articleBody) ||
        !is.null(x$headline) ||
        !is.null(x$datePublished)
      ) {
        
        body <- if (!is.null(x$articleBody)) {
          as.character(x$articleBody)
        } else {
          NA_character_
        }
        
        headline <- if (!is.null(x$headline)) {
          as.character(x$headline)
        } else {
          NA_character_
        }
        
        datePublished <- if (!is.null(x$datePublished)) {
          as.character(x$datePublished)
        } else {
          NA_character_
        }
        
        return(
          list(
            headline = headline,
            datePublished = datePublished,
            articleBody = body
          )
        )
      }
      
      
      # Search  for nested objects
      for (item in x) {
        
        result <- find_article_data(item)
        
        if (
          !is.null(result) &&
          (
            !is.na(result$articleBody) ||
            !is.na(result$headline)
          )
        ) {
          
          return(result)
        }
      }
    }
    
    
    NULL
  }
  
  
  # ----------------------------------------------------------
  # Try each JSON-LD script
  # ----------------------------------------------------------
  
  for (script in scripts) {
    
    parsed <- tryCatch(
      
      fromJSON(
        script,
        simplifyVector = FALSE
      ),
      
      error = function(e) {
        NULL
      }
    )
    
    
    if (!is.null(parsed)) {
      
      result <- find_article_data(
        parsed
      )
      
      
      if (!is.null(result)) {
        
        return(result)
      }
    }
  }
  
  
  list(
    headline = NA_character_,
    datePublished = NA_character_,
    articleBody = NA_character_
  )
}


# ============================================================
# 13. FUNCTION: SCRAPE 1 ARTICLE
# ============================================================

scrape_article <- function(url) {
  
  cat(
    "Scraping:",
    url,
    "\n"
  )
  
  
  tryCatch({
    
    # --------------------------------------------------------
    # Download article
    # --------------------------------------------------------
    
    response <- request(url) |>
      req_user_agent(user_agent) |>
      req_timeout(30) |>
      req_perform()
    
    
    html <- resp_body_html(
      response
    )
    
    
    # --------------------------------------------------------
    # JSON-LD
    # --------------------------------------------------------
    
    json_data <- extract_jsonld_article(
      html
    )
    
    
    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------
    
    title <- json_data$headline
    
    
    # Fallback to H1
    if (
      is.na(title) ||
      str_trim(title) == ""
    ) {
      
      h1 <- html_element(
        html,
        "h1"
      )
      
      
      if (length(h1) > 0) {
        
        title <- html_text2(
          h1
        )
        
      } else {
        
        title <- NA_character_
      }
    }
    
    
    # --------------------------------------------------------
    # DATE
    # --------------------------------------------------------
    
    date_raw <- json_data$datePublished
    
    
    # Fallback 1: meta
    if (
      is.na(date_raw) ||
      str_trim(date_raw) == ""
    ) {
      
      meta_date <- html |>
        html_elements(
          "meta[property='article:published_time']"
        ) |>
        html_attr(
          "content"
        )
      
      
      meta_date <- meta_date[
        !is.na(meta_date) &
          str_trim(meta_date) != ""
      ]
      
      
      if (length(meta_date) > 0) {
        
        date_raw <- meta_date[1]
      }
    }
    
    
    # Fallback 2: datePublished meta
    if (
      is.na(date_raw) ||
      str_trim(date_raw) == ""
    ) {
      
      meta_date <- html |>
        html_elements(
          "meta[itemprop='datePublished']"
        ) |>
        html_attr(
          "content"
        )
      
      
      meta_date <- meta_date[
        !is.na(meta_date) &
          str_trim(meta_date) != ""
      ]
      
      
      if (length(meta_date) > 0) {
        
        date_raw <- meta_date[1]
      }
    }
    
    
    # Fallback 3: visible date
    if (
      is.na(date_raw) ||
      str_trim(date_raw) == ""
    ) {
      
      page_text <- html |>
        html_element(
          "body"
        ) |>
        html_text2()
      
      
      date_match <- str_extract(
        page_text,
        "[0-9]{1,2}\\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\s+[0-9]{4}"
      )
      
      
      if (!is.na(date_match)) {
        
        date_raw <- date_match
      }
    }
    
    
    # --------------------------------------------------------
    # YEAR
    # --------------------------------------------------------
    
    year <- str_extract(
      date_raw,
      "(19|20)[0-9]{2}"
    )
    
    
    year <- suppressWarnings(
      as.integer(year)
    )
    
    
    # ========================================================
    # ARTICLE BODY
    # ========================================================
    
    # FIRST CHOICE:
    # JSON-LD articleBody
    # This avoids navigation/sidebar/header text.
    # --------------------------------------------------------
    
    article_text <- json_data$articleBody
    
    
    # --------------------------------------------------------
    # Clean JSON-LD text
    # --------------------------------------------------------
    
    if (
      !is.na(article_text) &&
      str_trim(article_text) != ""
    ) {
      
      article_text <- str_squish(
        article_text
      )
    }
    
    
    # ========================================================
    # FALLBACK CONTENT SELECTORS
    # ========================================================
    
    if (
      is.na(article_text) ||
      nchar(article_text) < 200
    ) {
      
      selectors <- c(
        
        "[itemprop='articleBody']",
        
        ".article-content",
        
        ".article_body",
        
        ".article-body",
        
        ".story-content",
        
        ".story_content",
        
        ".story-body",
        
        ".story_body",
        
        ".post-content",
        
        ".entry-content"
      )
      
      
      for (selector in selectors) {
        
        node <- html_element(
          html,
          selector
        )
        
        
        if (
          length(node) > 0 &&
          !is.na(node)
        ) {
          
          candidate <- html_text2(
            node
          )
          
          
          candidate <- str_squish(
            candidate
          )
          
          
          if (
            !is.na(candidate) &&
            nchar(candidate) >= 200
          ) {
            
            article_text <- candidate
            
            break
          }
        }
      }
    }
    
    
    # --------------------------------------------------------
    # Final fallback
    # --------------------------------------------------------
    
    if (
      is.na(article_text) ||
      nchar(article_text) < 200
    ) {
      
      article_text <- NA_character_
    }
    
    
    # ========================================================
    # RETURN
    # ========================================================
    
    tibble(
      
      article_url = url,
      
      title = title,
      
      date_raw = date_raw,
      
      year = year,
      
      article_text = article_text
    )
    
    
  }, error = function(e) {
    
    cat(
      "FAILED:",
      url,
      "\n"
    )
    
    cat(
      "Reason:",
      conditionMessage(e),
      "\n"
    )
    
    
    tibble(
      
      article_url = url,
      
      title = NA_character_,
      
      date_raw = NA_character_,
      
      year = NA_integer_,
      
      article_text = NA_character_
    )
  })
}


# ============================================================
# 14. TEST ONE ARTICLE FIRST
# ============================================================

cat("\n")
cat("============================================================\n")
cat("TESTING PAGE 31 ARTICLE\n")
cat("============================================================\n")


test_url <- all_page_articles |>
  filter(page == 31) |>
  pull(article_url) |>
  first()


test_article <- scrape_article(
  test_url
)


print(
  test_article |>
    select(
      title,
      date_raw,
      year
    )
)


cat("\nARTICLE TEXT LENGTH:\n")

print(
  nchar(
    test_article$article_text
  )
)


cat("\nFIRST 1000 CHARACTERS:\n")

cat(
  substr(
    test_article$article_text,
    1,
    1000
  )
)


# ============================================================
# 15. SCRAPE ALL ARTICLES
# ============================================================

cat("\n\n")
cat("============================================================\n")
cat("SCRAPING ALL ARTICLES\n")
cat("============================================================\n")


articles_raw <- map_dfr(
  article_urls$article_url,
  scrape_article
)


# ============================================================
# 16. REMOVE DUPLICATES
# ============================================================

articles_raw <- articles_raw |>
  distinct(
    article_url,
    .keep_all = TRUE
  )


# ============================================================
# 17. SAVE ALL SCRAPED ARTICLES
# ============================================================

write_csv(
  articles_raw,
  "newsmeter_health_all_articles_raw.csv"
)


# ============================================================
# 18. FILTER 2022–2025
# ============================================================

articles_2022_2025 <- articles_raw |>
  
  filter(
    !is.na(year),
    year >= 2022,
    year <= 2025
  ) |>
  
  distinct(
    article_url,
    .keep_all = TRUE
  ) |>
  
  arrange(
    year,
    date_raw
  )


# ============================================================
# 19. SAVE FINAL DATASET
# ============================================================

write_csv(
  articles_2022_2025,
  "newsmeter_health_articles_2022_2025.csv"
)


# ============================================================
# 20. YEARLY COUNTS
# ============================================================

yearly_counts <- articles_2022_2025 |>
  
  count(
    year,
    name = "n"
  ) |>
  
  arrange(year)


cat("\n")
cat("============================================================\n")
cat("NEWSMETER HEALTH — 2022–2025\n")
cat("============================================================\n")

print(yearly_counts)


# ============================================================
# 21. TOTAL
# ============================================================

cat("\nTOTAL 2022–2025 ARTICLES:\n")

print(
  nrow(articles_2022_2025)
)


# ============================================================
# 22. MISSING YEAR CHECK
# ============================================================

cat("\n")
cat("============================================================\n")
cat("ARTICLES WITH MISSING YEAR\n")
cat("============================================================\n")


missing_year <- articles_raw |>
  
  filter(
    is.na(year)
  ) |>
  
  select(
    article_url,
    title,
    date_raw
  )


print(
  missing_year
)


# ============================================================
# 23. MISSING ARTICLE TEXT CHECK
# ============================================================

cat("\n")
cat("============================================================\n")
cat("ARTICLES WITH MISSING ARTICLE TEXT\n")
cat("============================================================\n")


missing_text <- articles_raw |>
  
  mutate(
    text_length = nchar(article_text)
  ) |>
  
  filter(
    is.na(article_text) |
      text_length < 200
  ) |>
  
  select(
    article_url,
    title,
    year,
    text_length
  )


print(
  missing_text
)


# ============================================================
# 24. FINAL SUMMARY
# ============================================================

cat("\n")
cat("============================================================\n")
cat("FINAL SUMMARY\n")
cat("============================================================\n")

cat(
  "Pages searched       : 1–31\n",
  "Article-link count   : ",
  sum(page_counts$article_count),
  "\n",
  "Unique URLs          : ",
  nrow(article_urls),
  "\n",
  "Articles scraped     : ",
  nrow(articles_raw),
  "\n",
  "2022–2025 articles   : ",
  nrow(articles_2022_2025),
  "\n",
  sep = ""
)

cat("\nYEARLY COUNTS:\n")

print(
  yearly_counts
)

write_csv(articles_2022_2025,"C:\\Users\\gowri\\OneDrive\\Desktop\\summer_internship\\newsmeter_titles.csv")
